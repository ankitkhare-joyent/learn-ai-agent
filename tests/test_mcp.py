import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import MCP, token


async def _session_call(tok, name, args):
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"})
    async with http, streamable_http_client(MCP, http_client=http) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        return await s.list_tools(), await s.call_tool(name, args)


def test_mcp_rejects_missing_and_wrong_audience_tokens():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(MCP, json=body).status_code == 401
    wrong = {"Authorization": f"Bearer {token(audience='ec2-api')}"}   # an API token must not work here
    assert httpx.post(MCP, json=body, headers=wrong).status_code == 401


def test_protected_resource_metadata():
    meta = httpx.get("http://localhost:8001/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == MCP and meta["authorization_servers"]


async def test_tools_are_read_only_and_work():
    tools, result = await _session_call(token(audience="ec2-mcp"), "fleet_summary", {"group_by": "state"})
    assert {t.name for t in tools.tools} == {"get_instance", "find_instances", "fleet_summary", "instance_history"}
    assert all(t.annotations.readOnlyHint for t in tools.tools)
    assert not result.isError and result.structuredContent["groups"]


async def test_tool_errors_are_actionable():
    _, result = await _session_call(token(audience="ec2-mcp"), "get_instance", {"instance_id": "i-00000000000dead0"})
    assert result.isError and "not found" in result.content[0].text


async def test_user_permissions_flow_through_mcp(reader):
    """The reader lacks inventory:history; token exchange keeps that restriction end to end."""
    _, result = await _session_call(token(reader, audience="ec2-mcp"), "instance_history",
                                    {"instance_id": "i-0000000000000abc"})
    assert result.isError and "permission denied" in result.content[0].text
