"""bao_exec: log in to OpenBao with AppRole, read secrets into environment variables, then exec the real command.

    BAO_SECRETS="API_JWT_PRIVATE_KEY=ec2-lakehouse/api#jwt_private_key;S3_ACCESS_KEY=ec2-lakehouse/s3#access_key" \
    python bao_exec.py uvicorn api.app.main:app ...

The app itself never knows OpenBao exists: it reads env vars like any 12-factor app. This is the same idea as
OpenBao Agent's `exec` mode or envconsul (and, on Kubernetes, the External Secrets Operator syncing into a
Secret). Stdlib only, so it can be copied into any Python image.

Trade-off: secrets are read once at start. Rotating a secret means restarting the container (fine for most
apps; use OpenBao Agent with templates if you need live reload).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADDR = os.environ.get("BAO_ADDR", "http://openbao:8200")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(f"{ADDR}/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-Vault-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: bao_exec.py <command> [args...]")
    role_id = Path(os.environ.get("BAO_ROLE_ID_FILE", "/creds/role_id")).read_text().strip()
    secret_id = Path(os.environ.get("BAO_SECRET_ID_FILE", "/creds/secret_id")).read_text().strip()
    for attempt in range(30):   # OpenBao may still be unsealing when we start
        try:
            token = call("POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id})["auth"]["client_token"]
            break
        except (urllib.error.URLError, OSError) as e:
            print(f"bao_exec: waiting for OpenBao ({e})", file=sys.stderr, flush=True)
            time.sleep(2)
    else:
        sys.exit("bao_exec: could not log in to OpenBao")

    cache: dict[str, dict] = {}
    for item in filter(None, (i.strip() for i in os.environ.get("BAO_SECRETS", "").split(";"))):
        env_name, ref = item.split("=", 1)
        path, key = ref.split("#", 1)
        if path not in cache:
            cache[path] = call("GET", f"secret/data/{path}", token=token)["data"]["data"]
        os.environ[env_name.strip()] = cache[path][key]
    try:   # the token was only needed to read; revoke it so it can't be reused
        call("POST", "auth/token/revoke-self", {}, token=token)
    except urllib.error.URLError:
        pass
    print(f"bao_exec: loaded {len(cache)} secret path(s), starting {sys.argv[1]}", file=sys.stderr, flush=True)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
