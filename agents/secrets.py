"""Read secrets from OpenBao on your laptop (agents, tests, scripts).

Token lookup order: BAO_TOKEN env var, then ./.bao/dev-token (written by `mise run up`).
In a real org you'd run `bao login -method=oidc` once a day instead of keeping a token file.
"""
import os
from functools import lru_cache
from pathlib import Path

import httpx

BAO_ADDR = os.environ.get("BAO_ADDR", "http://localhost:8200")
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".bao" / "dev-token"


def _token() -> str | None:
    if os.environ.get("BAO_TOKEN"):
        return os.environ["BAO_TOKEN"]
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else None


@lru_cache
def read_secret(path: str) -> dict:
    """KV v2 read, e.g. read_secret("ec2-lakehouse/agent") -> {"username": ..., "password": ...}. {} if missing."""
    token = _token()
    if not token:
        return {}
    r = httpx.get(f"{BAO_ADDR}/v1/secret/data/{path}", headers={"X-Vault-Token": token}, timeout=5)
    return r.json()["data"]["data"] if r.status_code == 200 else {}


def write_secret(path: str, data: dict) -> None:
    r = httpx.post(f"{BAO_ADDR}/v1/secret/data/{path}", headers={"X-Vault-Token": _token() or ""},
                   json={"data": data}, timeout=5)
    r.raise_for_status()
