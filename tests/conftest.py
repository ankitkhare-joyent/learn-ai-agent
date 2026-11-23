"""Integration tests run against the Docker Compose stack (`mise run up` first). They skip if it isn't running."""
import httpx
import pytest

from agents.secrets import read_secret

API = "http://localhost:8000"
MCP = "http://localhost:8001/mcp"
ADMIN = ("admin", read_secret("ec2-lakehouse/admin").get("password", ""))


def _up() -> bool:
    try:
        return httpx.get(f"{API}/readyz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def pytest_collection_modifyitems(config, items):
    if not _up():
        skip = pytest.mark.skip(reason="stack not running: `mise run up`")
        for item in items:
            item.add_marker(skip)


def token(user=ADMIN, audience="ec2-api", scope="") -> str:
    r = httpx.post(f"{API}/auth/token", data={"username": user[0], "password": user[1], "audience": audience,
                                               "scope": scope})
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_headers():
    return {"Authorization": f"Bearer {token()}"}


@pytest.fixture(scope="session")
def reader():
    """A user with only inventory:read (created once; idempotent across runs)."""
    creds = ("reader-test", "reader-test-password-123")
    httpx.post(f"{API}/admin/users", headers={"Authorization": f"Bearer {token()}"},
               json={"username": creds[0], "password": creds[1], "scopes": ["inventory:read"]})
    return creds
