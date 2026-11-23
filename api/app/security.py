"""Users, passwords, JWT access tokens, scopes and token exchange.

Production notes (see docs/03-rest-api.md):
  * In a real deployment you would NOT run your own user database and password
    login. Put the API behind your IdP (Okta / Entra ID / Cognito / Keycloak) and
    only *validate* its tokens here. This module shows what that IdP does for you,
    so the moving parts are visible: hashing, short-lived signed tokens, audiences,
    scopes, and token exchange.
  * Tokens carry an *audience*. A token minted for the MCP server (aud=ec2-mcp)
    is rejected by this API, and vice versa. The MCP server swaps its token for an
    API token with RFC 8693 token exchange instead of forwarding it
    ("token passthrough" is explicitly forbidden by the MCP authorization spec).
"""
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes
from jwt.algorithms import RSAAlgorithm
from pwdlib import PasswordHash

from .settings import get_settings

API_AUDIENCE = "ec2-api"
MCP_AUDIENCE = "ec2-mcp"
AUDIENCES = (API_AUDIENCE, MCP_AUDIENCE)

SCOPES = {
    "inventory:read": "Read the latest EC2 inventory snapshot",
    "inventory:history": "Read historical snapshots and per-instance history",
    "users:admin": "Create, list and disable users",
}

password_hash = PasswordHash.recommended()  # Argon2id
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", scopes=SCOPES)
_db_lock = threading.Lock()


# ------------------------------------------------------------------ user store
def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(get_settings().users_db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_user_store() -> None:
    s = get_settings()
    with _db_lock, _conn() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, scopes TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
        bootstrap = [(s.admin_username, s.admin_password, list(SCOPES))]
        if s.agent_username and s.agent_password:
            bootstrap.append((s.agent_username, s.agent_password, ["inventory:read", "inventory:history"]))
        for username, password, scopes in bootstrap:
            # Upsert, so a password rotated in OpenBao takes effect on restart. Several workers start at once.
            con.execute("""INSERT INTO users VALUES (?, ?, ?, 0, ?) ON CONFLICT(username)
                           DO UPDATE SET password_hash = excluded.password_hash, scopes = excluded.scopes""",
                        (username, password_hash.hash(password.get_secret_value()), " ".join(scopes),
                         datetime.now(UTC).isoformat()))


def get_user(username: str) -> sqlite3.Row | None:
    with _conn() as con:
        return con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT username, scopes, disabled, created_at FROM users ORDER BY username").fetchall()
    return [{"username": r["username"], "scopes": r["scopes"].split(), "disabled": bool(r["disabled"]),
             "created_at": r["created_at"]} for r in rows]


def create_user(username: str, password: str, scopes: list[str]) -> None:
    unknown = set(scopes) - set(SCOPES)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown scopes: {sorted(unknown)}")
    with _db_lock, _conn() as con:
        try:
            con.execute("INSERT INTO users VALUES (?, ?, ?, 0, ?)",
                        (username, password_hash.hash(password), " ".join(scopes), datetime.now(UTC).isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, "user already exists") from None


def set_disabled(username: str, disabled: bool) -> None:
    with _db_lock, _conn() as con:
        if con.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username)).rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")


# Hash of a random password, so a login for an unknown user costs the same as a real one (no user enumeration).
_DUMMY_HASH = password_hash.hash(secrets.token_urlsafe(16))


def authenticate(username: str, password: str) -> sqlite3.Row | None:
    user = get_user(username)
    if user is None:
        password_hash.verify(password, _DUMMY_HASH)
        return None
    if user["disabled"] or not password_hash.verify(password, user["password_hash"]):
        return None
    return user


# ------------------------------------------------------------------ tokens
@lru_cache
def _keys():
    """(private key, public key, kid). The kid lets verifiers pick the right key during key rotation."""
    private = load_pem_private_key(get_settings().jwt_private_key.get_secret_value().encode(), password=None)
    public = private.public_key()
    jwk = RSAAlgorithm.to_jwk(public, as_dict=True)
    kid = hashlib.sha256(json.dumps({k: jwk[k] for k in ("e", "kty", "n")}, sort_keys=True).encode()).hexdigest()[:16]
    return private, public, kid


def jwks() -> dict:
    _, public, kid = _keys()
    return {"keys": [{**RSAAlgorithm.to_jwk(public, as_dict=True), "kid": kid, "use": "sig",
                      "alg": "RS256"}]}


def issue_token(subject: str, scopes: list[str], audience: str, actor: str | None = None) -> tuple[str, int]:
    s = get_settings()
    now = datetime.now(UTC)
    ttl = timedelta(minutes=s.access_token_minutes)
    claims = {"iss": s.jwt_issuer, "sub": subject, "aud": audience, "scope": " ".join(scopes),
              "iat": now, "nbf": now, "exp": now + ttl, "jti": str(uuid.uuid4())}
    if actor:  # RFC 8693 "act" claim: who is acting on the user's behalf (the MCP server)
        claims["act"] = {"sub": actor}
    private, _, kid = _keys()
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid}), int(ttl.total_seconds())


def decode_token(token: str, audience: str) -> dict:
    s = get_settings()
    try:
        return jwt.decode(token, _keys()[1], algorithms=["RS256"], audience=audience,
                          issuer=s.jwt_issuer, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}",
                            headers={"WWW-Authenticate": "Bearer"}) from None


class Principal(dict):
    @property
    def username(self) -> str:
        return self["sub"]

    @property
    def scopes(self) -> set[str]:
        return set(self.get("scope", "").split())


def current_principal(security_scopes: SecurityScopes, token: Annotated[str, Depends(oauth2_scheme)]) -> Principal:
    """FastAPI dependency: validates the bearer token and the scopes the route asks for."""
    claims = Principal(decode_token(token, API_AUDIENCE))
    user = get_user(claims.username)
    if user is None or user["disabled"]:  # disabling a user takes effect before their token expires
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled", headers={"WWW-Authenticate": "Bearer"})
    missing = set(security_scopes.scopes) - claims.scopes
    if missing:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope(s): {' '.join(sorted(missing))}",
                            headers={"WWW-Authenticate": f'Bearer scope="{security_scopes.scope_str}"'})
    return claims
