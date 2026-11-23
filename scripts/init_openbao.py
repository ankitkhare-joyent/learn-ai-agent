"""Initialize, unseal and configure OpenBao for the stack. Idempotent: safe to run on every `compose up`.

What it sets up (docs/09-secrets-openbao.md walks through each piece):

  secret/ (KV v2)                         who can read it (policy)
    ec2-lakehouse/s3          S3 keys                              pipeline, api, developer
    ec2-lakehouse/api         JWT signing key                      api
    ec2-lakehouse/admin       API admin user + password            api, developer
    ec2-lakehouse/mcp-client  OAuth client secret for the MCP server   api, mcp
    ec2-lakehouse/agent       the agent's API user + password       api, developer
    ec2-lakehouse/anthropic   ANTHROPIC_API_KEY (you write it)      developer

  auth/approle   one role per workload (pipeline, api, mcp): machine login, short-lived tokens
  a developer token in ./.bao/dev-token for agents you run on your laptop

Secrets are generated randomly the first time and never overwritten (KV "check-and-set" = 0).

LOCAL-ONLY SHORTCUTS (not for production): one unseal key and the root token are kept in a Docker volume;
TLS is off. Production: KMS auto-unseal, root token revoked after setup, TLS everywhere, Kubernetes auth
instead of AppRole secret-id files, humans log in with OIDC.
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

BAO = os.environ.get("BAO_ADDR", "http://openbao:8200")
KEYS_FILE = Path(os.environ.get("BAO_KEYS_FILE", "/bao-keys/init.json"))
CREDS_DIR = Path(os.environ.get("BAO_CREDS_DIR", "/creds"))
DEV_TOKEN_FILE = Path(os.environ.get("BAO_DEV_TOKEN_FILE", "/dev-out/dev-token"))

POLICIES = {
    "pipeline": ['path "secret/data/ec2-lakehouse/s3" { capabilities = ["read"] }'],
    "api": ['path "secret/data/ec2-lakehouse/s3" { capabilities = ["read"] }',
            'path "secret/data/ec2-lakehouse/api" { capabilities = ["read"] }',
            'path "secret/data/ec2-lakehouse/admin" { capabilities = ["read"] }',
            'path "secret/data/ec2-lakehouse/mcp-client" { capabilities = ["read"] }',
            'path "secret/data/ec2-lakehouse/agent" { capabilities = ["read"] }'],
    "mcp": ['path "secret/data/ec2-lakehouse/mcp-client" { capabilities = ["read"] }'],
    # Humans: read what they need to use the stack locally (incl. S3 keys for other local projects), and
    # write their own Anthropic key. Never the API's signing key.
    "developer": ['path "secret/data/ec2-lakehouse/agent" { capabilities = ["read"] }',
                  'path "secret/data/ec2-lakehouse/s3" { capabilities = ["read"] }',
                  'path "secret/data/ec2-lakehouse/admin" { capabilities = ["read"] }',
                  'path "secret/data/ec2-lakehouse/anthropic" { capabilities = ["create", "read", "update"] }',
                  'path "secret/metadata/ec2-lakehouse/*" { capabilities = ["list", "read"] }'],
}
APPROLES = ("pipeline", "api", "mcp")


def call(method: str, path: str, body: dict | None = None, token: str | None = None, ok=(200, 204)) -> dict:
    req = urllib.request.Request(f"{BAO}/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-Vault-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code in ok:
            return {}
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def wait_for_bao():
    for _ in range(60):
        try:
            call("GET", "sys/health?sealedcode=200&uninitcode=200")
            return
        except Exception:
            time.sleep(1)
    raise SystemExit("OpenBao did not come up")


def init_and_unseal() -> str:
    if not call("GET", "sys/init")["initialized"]:
        out = call("PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1})
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEYS_FILE.write_text(json.dumps({"unseal_key": out["keys_base64"][0], "root_token": out["root_token"]}))
        KEYS_FILE.chmod(0o600)
        print("initialized OpenBao (1 key share - local only)")
    keys = json.loads(KEYS_FILE.read_text())
    if call("GET", "sys/seal-status")["sealed"]:
        call("PUT", "sys/unseal", {"key": keys["unseal_key"]})
        print("unsealed")
    for _ in range(30):   # raft needs a moment to elect itself leader after unseal
        if call("GET", "sys/health?standbyok=true&sealedcode=200&uninitcode=200").get("sealed") is False:
            try:
                call("GET", "sys/mounts", token=keys["root_token"])
                break
            except RuntimeError:
                pass
        time.sleep(1)
    return keys["root_token"]


def enable(root: str, kind: str, path: str, body: dict):
    existing = call("GET", f"sys/{kind}", token=root)
    if f"{path}/" not in existing.get("data", existing):
        call("POST", f"sys/{kind}/{path}", body, token=root)
        print(f"enabled {kind.removesuffix('s')} {path}/")


def put_once(root: str, path: str, data: dict):
    """Write a secret only if it doesn't exist yet (check-and-set 0), so restarts never rotate it by accident."""
    try:
        call("POST", f"secret/data/{path}", {"options": {"cas": 0}, "data": data}, token=root)
        print(f"created secret/{path}")
    except RuntimeError as e:
        if "check-and-set" not in str(e):
            raise


def rsa_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def main():
    wait_for_bao()
    root = init_and_unseal()
    enable(root, "mounts", "secret", {"type": "kv", "options": {"version": "2"}})
    enable(root, "auth", "approle", {"type": "approle"})
    for name, rules in POLICIES.items():
        call("PUT", f"sys/policies/acl/{name}", {"policy": "\n".join(rules)}, token=root)

    put_once(root, "ec2-lakehouse/s3", {"access_key": "lakehouse-" + secrets.token_hex(4),
                                        "secret_key": secrets.token_urlsafe(32)})
    put_once(root, "ec2-lakehouse/api", {"jwt_private_key": rsa_private_pem()})
    put_once(root, "ec2-lakehouse/admin", {"username": "admin", "password": secrets.token_urlsafe(18)})
    put_once(root, "ec2-lakehouse/mcp-client", {"client_id": "ec2-mcp", "client_secret": secrets.token_urlsafe(32)})
    put_once(root, "ec2-lakehouse/agent", {"username": "agent", "password": secrets.token_urlsafe(18)})

    # AppRole per workload. Each gets its role_id + a fresh secret_id in its own folder, which compose mounts
    # into only that container (the same job the Kubernetes auth method / OpenBao Agent does in a cluster).
    for role in APPROLES:
        call("POST", f"auth/approle/role/{role}", {"token_policies": role, "token_ttl": "1h", "token_max_ttl": "24h",
                                                   "secret_id_ttl": "720h", "secret_id_num_uses": 0}, token=root)
        role_id = call("GET", f"auth/approle/role/{role}/role-id", token=root)["data"]["role_id"]
        secret_id = call("POST", f"auth/approle/role/{role}/secret-id", {}, token=root)["data"]["secret_id"]
        d = CREDS_DIR / role
        d.mkdir(parents=True, exist_ok=True)
        (d / "role_id").write_text(role_id)
        (d / "secret_id").write_text(secret_id)

    # SeaweedFS reads S3 identities from a file; render it from the secret (OpenBao Agent templates do this in prod).
    s3 = call("GET", "secret/data/ec2-lakehouse/s3", token=root)["data"]["data"]
    iam = {"identities": [{"name": "lakehouse", "credentials": [{"accessKey": s3["access_key"],
                                                                 "secretKey": s3["secret_key"]}],
                           "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}
    (CREDS_DIR / "seaweedfs").mkdir(parents=True, exist_ok=True)
    (CREDS_DIR / "seaweedfs" / "iam.json").write_text(json.dumps(iam))

    # A developer token for the agents you run on the host (prod: `bao login -method=oidc` via your IdP).
    valid = False
    if DEV_TOKEN_FILE.exists():
        try:
            call("GET", "auth/token/lookup-self", token=DEV_TOKEN_FILE.read_text().strip())
            valid = True
        except RuntimeError:
            pass
    if not valid and DEV_TOKEN_FILE.parent.exists():
        tok = call("POST", "auth/token/create", {"policies": ["developer"], "ttl": "720h", "renewable": True,
                                                 "display_name": "developer", "no_parent": True}, token=root)
        DEV_TOKEN_FILE.write_text(tok["auth"]["client_token"])
        DEV_TOKEN_FILE.chmod(0o600)
        print(f"wrote developer token to {DEV_TOKEN_FILE}")
    print("OpenBao ready")


if __name__ == "__main__":
    main()
