import httpx

from agents.secrets import read_secret
from tests.conftest import API, token


def test_requires_auth():
    assert httpx.get(f"{API}/v1/ec2/instances").status_code == 401


def test_bad_password_is_rejected():
    r = httpx.post(f"{API}/auth/token", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_list_filter_sort(admin_headers):
    r = httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers, params={
        "account_id": "392918273645", "state": "running", "sort_by": "vcpus", "descending": True, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0 and len(body["items"]) == 5
    vcpus = [i["vcpus"] for i in body["items"]]
    assert vcpus == sorted(vcpus, reverse=True)
    assert all(i["account_id"] == "392918273645" and i["state"] == "running" for i in body["items"])


def test_validation_errors_are_422(admin_headers):
    assert httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers, params={"account_id": "123"}).status_code == 422
    assert httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers, params={"limit": 5000}).status_code == 422


def test_get_instance_and_history(admin_headers):
    first = httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers, params={"limit": 1}).json()["items"][0]
    r = httpx.get(f"{API}/v1/ec2/instances/{first['instance_id']}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["in_latest_snapshot"] is True
    h = httpx.get(f"{API}/v1/ec2/instances/{first['instance_id']}/history", headers=admin_headers)
    assert h.status_code == 200 and h.json()["changes"]


def test_terminated_instance_found_in_history(admin_headers):
    """An instance terminated a few days before the latest snapshot is found via the Iceberg history path."""
    snaps = httpx.get(f"{API}/v1/ec2/snapshots", headers=admin_headers, params={"days": 3}).json()["snapshots"]
    old = snaps[-1]["snapshot_ts"]
    old_ids = {i["instance_id"] for i in httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers,
               params={"snapshot_ts": old, "service": "spark-batch", "limit": 500}).json()["items"]}
    now_ids = {i["instance_id"] for i in httpx.get(f"{API}/v1/ec2/instances", headers=admin_headers,
               params={"service": "spark-batch", "limit": 500}).json()["items"]}
    gone = sorted(old_ids - now_ids)
    assert gone, "expected spot/batch churn between snapshots"
    r = httpx.get(f"{API}/v1/ec2/instances/{gone[0]}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["in_latest_snapshot"] is False


def test_scopes_enforced(reader):
    h = {"Authorization": f"Bearer {token(reader)}"}
    assert httpx.get(f"{API}/v1/ec2/summary", headers=h).status_code == 200
    assert httpx.get(f"{API}/v1/ec2/snapshots", headers=h).status_code == 403          # needs inventory:history
    assert httpx.get(f"{API}/admin/users", headers=h).status_code == 403               # needs users:admin


def test_mcp_audience_token_rejected_by_api():
    h = {"Authorization": f"Bearer {token(audience='ec2-mcp')}"}
    assert httpx.get(f"{API}/v1/ec2/summary", headers=h).status_code == 401


def test_token_exchange_requires_client_auth():
    mcp_token = token(audience="ec2-mcp")
    form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": mcp_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token", "audience": "ec2-api"}
    assert httpx.post(f"{API}/auth/token/exchange", data=form, auth=("ec2-mcp", "wrong")).status_code == 401
    assert read_secret("ec2-lakehouse/mcp-client") == {}, "developers must not be able to read the MCP client secret"


def test_jwks_published_and_tokens_are_rs256():
    import jwt
    keys = httpx.get(f"{API}/.well-known/jwks.json").json()["keys"]
    header = jwt.get_unverified_header(token())
    assert header["alg"] == "RS256" and header["kid"] in {k["kid"] for k in keys}
