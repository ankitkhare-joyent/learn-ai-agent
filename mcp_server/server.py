"""EC2 inventory MCP server: the REST API's capabilities as MCP tools, resources and prompts.

    python server.py --transport stdio   # local: spawned by Claude Desktop / Claude Code / an agent
    python server.py --transport http    # remote: Streamable HTTP on :8001/mcp, OAuth-protected

Design rules applied here (docs/06-mcp-servers.md explains each):
  1. Tools are shaped around *tasks an agent has*, not 1:1 around endpoints, and there are few of them.
  2. Every tool is read-only and annotated as such; nothing can mutate infrastructure.
  3. Inputs are typed and constrained (Literal / Field) so the model gets a precise JSON schema.
  4. Outputs are compact, structured and bounded (max rows), because every byte lands in the model's context.
  5. Errors are returned as actionable text so the model can correct itself ("account_id must be 12 digits").
  6. Over HTTP the server is an OAuth 2.1 resource server: it validates tokens issued *for it* (audience),
     then exchanges them for an API token (RFC 8693). It never forwards the user's token (no token passthrough).
"""
import argparse
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
import jwt
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("ec2-mcp")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_", extra="ignore")
    api_base_url: str = "http://localhost:8000"
    # HTTP mode: validate user tokens (aud=ec2-mcp) and exchange them for API tokens.
    jwks_url: str = "http://api:8000/.well-known/jwks.json"   # the issuer's public keys (no shared secret)
    jwt_issuer: str = "ec2-inventory-api"
    issuer_url: str = "http://localhost:8000"          # the OAuth authorization server
    resource_url: str = "http://localhost:8001/mcp"    # this server's identity (RFC 8707 / 9728)
    client_id: str = "ec2-mcp"
    client_secret: str = ""
    # stdio mode: credentials come from the environment (the MCP spec says stdio servers must not do OAuth).
    api_username: str = ""
    api_password: str = ""
    http_timeout_seconds: float = 20.0
    allowed_hosts: list[str] = ["localhost:8001", "127.0.0.1:8001", "mcp:8001"]


settings = Settings()
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
MAX_ROWS = 50


# ------------------------------------------------------------------ auth: token verification + exchange
class JwtVerifier:
    """Accepts only tokens minted for this MCP server (aud=ec2-mcp) by the trusted issuer.

    Signatures are checked against the issuer's JWKS (fetched and cached by PyJWKClient), exactly as you would
    with Okta / Entra ID / Cognito in production.
    """

    def __init__(self):
        self.jwks = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=300)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key, algorithms=["RS256"], audience="ec2-mcp", issuer=settings.jwt_issuer)
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            return None
        return AccessToken(token=token, client_id=claims.get("client_id", "unknown"), subject=claims["sub"],
                           scopes=claims.get("scope", "").split(), expires_at=claims["exp"],
                           resource=settings.resource_url, claims=claims)


@dataclass
class ApiToken:
    value: str
    expires_at: float


class InventoryApi:
    """Thin async client for the REST API. One pooled httpx client for the server's lifetime."""

    def __init__(self, mode: str):
        self.mode = mode
        self.http = httpx.AsyncClient(base_url=settings.api_base_url, timeout=settings.http_timeout_seconds)
        self._tokens: dict[str, ApiToken] = {}   # user token -> exchanged API token (cached until near expiry)
        self._lock = asyncio.Lock()

    async def _api_token(self) -> str:
        if self.mode == "stdio":
            key = "stdio"
            form = {"username": settings.api_username, "password": settings.api_password}
            url, auth = "/auth/token", None
        else:
            user = get_access_token()
            if user is None:  # the bearer middleware already rejects this; defence in depth
                raise ToolError("not authenticated")
            key = user.token
            form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": user.token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token", "audience": "ec2-api"}
            url, auth = "/auth/token/exchange", (settings.client_id, settings.client_secret)
        async with self._lock:
            cached = self._tokens.get(key)
            if cached and cached.expires_at - time.time() > 60:
                return cached.value
            r = await self.http.post(url, data=form, auth=auth)
            if r.status_code != 200:
                raise ToolError(f"could not obtain an API token ({r.status_code}): {r.text[:200]}")
            body = r.json()
            self._tokens[key] = ApiToken(body["access_token"], time.time() + body["expires_in"])
            return body["access_token"]

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        request_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {await self._api_token()}", "x-request-id": request_id}
        t0 = time.perf_counter()
        try:
            r = await self.http.get(path, params=params, headers=headers)
        except httpx.TimeoutException:
            raise ToolError("the inventory API timed out; try a narrower filter") from None
        log.info("api call path=%s status=%s ms=%.0f request_id=%s", path, r.status_code,
                 (time.perf_counter() - t0) * 1000, request_id)
        if r.status_code == 200:
            return r.json()
        # Turn API errors into messages a model can act on.
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        if r.status_code == 404:
            raise ToolError(f"not found: {detail}")
        if r.status_code == 403:
            raise ToolError(f"permission denied: {detail}. The user lacks this permission; tell them instead of "
                            "retrying.")
        if r.status_code == 422:
            raise ToolError(f"invalid arguments: {detail}")
        raise ToolError(f"inventory API error {r.status_code} (request_id={request_id})")


# ------------------------------------------------------------------ output models (become outputSchema)
class InstanceSummary(BaseModel):
    instance_id: str
    name: str | None
    state: str
    instance_type: str
    vcpus: int | None
    memory_gib: float | None
    gpus: int | None
    account_id: str
    region: str
    availability_zone: str | None
    team: str | None
    service: str | None
    cost_center: str | None
    lifecycle: str | None
    launch_time: str | None
    private_ip: str | None
    public_ip: str | None


class InstanceResult(BaseModel):
    instance: InstanceSummary
    in_latest_snapshot: bool = Field(description="False means the instance is gone (e.g. terminated)")
    observed_at: str = Field(description="Snapshot time (UTC) this record comes from")
    latest_snapshot_ts: str | None


class FindResult(BaseModel):
    snapshot_ts: str | None
    total_matches: int
    returned: int
    instances: list[InstanceSummary]
    note: str | None = None


class SummaryResult(BaseModel):
    snapshot_ts: str | None
    group_by: str
    groups: list[dict]


class HistoryResult(BaseModel):
    instance_id: str
    first_seen: str | None
    last_seen: str | None
    changes: list[dict]


def _summ(i: dict) -> InstanceSummary:
    return InstanceSummary(**{k: i.get(k) for k in InstanceSummary.model_fields})


# ------------------------------------------------------------------ server
@asynccontextmanager
async def lifespan(server: MCPServer):
    api = InventoryApi(server_mode)
    try:
        yield {"api": api}
    finally:
        await api.http.aclose()


server_mode = "stdio"  # set by build_server(); read by lifespan()


def build_server(mode: str) -> MCPServer:
    global server_mode
    server_mode = mode
    auth_kwargs = {}
    if mode == "http":
        auth_kwargs = dict(
            token_verifier=JwtVerifier(),
            auth=AuthSettings(issuer_url=settings.issuer_url, resource_server_url=settings.resource_url,
                              required_scopes=["inventory:read"], validate_token_resource=True),
        )
    mcp = MCPServer(
        name="ec2-inventory",
        title="EC2 Inventory",
        version="1.0.0",
        instructions=(
            "Read-only AWS EC2 inventory for all company accounts, snapshotted every 2 hours. "
            "Use get_instance for a specific instance ID, find_instances to search/rank, fleet_summary for counts "
            "and capacity, instance_history for how one instance changed over time. Data is as of the snapshot "
            "time in each result - always tell the user that time."),
        lifespan=lifespan,
        **auth_kwargs,
    )

    def api_from(ctx) -> InventoryApi:
        return ctx.request_context.lifespan_context["api"]

    @mcp.tool(annotations=READ_ONLY, title="Get one EC2 instance")
    async def get_instance(
        instance_id: Annotated[str, Field(pattern=r"^i-[0-9a-f]{8,17}$", description="EC2 instance ID, e.g. i-0abc...")],
        ctx: Context,
    ) -> InstanceResult:
        """Look up one EC2 instance by ID: state, region/AZ, account, type and size, team, IPs.

        If the instance no longer exists (terminated), returns its last known record from history.
        """
        body = await api_from(ctx).get(f"/v1/ec2/instances/{instance_id}")
        inst = body["instance"]
        return InstanceResult(instance=_summ(inst), in_latest_snapshot=body["in_latest_snapshot"],
                              observed_at=inst["snapshot_ts"], latest_snapshot_ts=body["latest_snapshot_ts"])

    @mcp.tool(annotations=READ_ONLY, title="Find / rank EC2 instances")
    async def find_instances(
        ctx: Context,
        account_id: Annotated[str | None, Field(pattern=r"^\d{12}$", description="12-digit AWS account ID")] = None,
        region: Annotated[str | None, Field(description="AWS region, e.g. us-east-1")] = None,
        state: Literal["pending", "running", "stopping", "stopped", "shutting-down", "terminated"] | None = None,
        instance_type: Annotated[str | None, Field(description="Exact type, e.g. m7i.large")] = None,
        team: str | None = None,
        service: str | None = None,
        name_contains: str | None = None,
        sort_by: Literal["vcpus", "memory_gib", "launch_time", "instance_id"] = "instance_id",
        descending: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_ROWS)] = 10,
    ) -> FindResult:
        """Search the latest EC2 snapshot with filters, sorted and limited.

        For "largest"/"biggest" instances use sort_by="vcpus" (or "memory_gib") with descending=true.
        For "newest" use sort_by="launch_time", descending=true. Returns at most `limit` rows plus the total count.
        """
        body = await api_from(ctx).get("/v1/ec2/instances", {
            "account_id": account_id, "region": region, "state": state, "instance_type": instance_type,
            "team": team, "service": service, "name_contains": name_contains, "sort_by": sort_by,
            "descending": descending, "limit": limit})
        items = [_summ(i) for i in body["items"]]
        note = None
        if body["total"] > len(items):
            note = f"Showing {len(items)} of {body['total']} matches; narrow the filters or raise limit (max {MAX_ROWS})."
        return FindResult(snapshot_ts=body["snapshot_ts"], total_matches=body["total"], returned=len(items),
                          instances=items, note=note)

    @mcp.tool(annotations=READ_ONLY, title="Fleet summary")
    async def fleet_summary(
        ctx: Context,
        group_by: Literal["region", "account_id", "state", "instance_type", "instance_family", "team", "service",
                          "lifecycle"] = "region",
        account_id: Annotated[str | None, Field(pattern=r"^\d{12}$")] = None,
        region: str | None = None,
        state: Literal["pending", "running", "stopping", "stopped", "shutting-down", "terminated"] | None = None,
    ) -> SummaryResult:
        """Instance counts and total capacity (vCPUs, memory GiB, GPUs) in the latest snapshot, grouped by one
        dimension. Use it for "how many", "which account/region has the most", and capacity questions."""
        body = await api_from(ctx).get("/v1/ec2/summary", {"group_by": group_by, "account_id": account_id,
                                                           "region": region, "state": state})
        return SummaryResult(snapshot_ts=body["snapshot_ts"], group_by=group_by, groups=body["rows"])

    @mcp.tool(annotations=READ_ONLY, title="Instance history")
    async def instance_history(
        instance_id: Annotated[str, Field(pattern=r"^i-[0-9a-f]{8,17}$")],
        ctx: Context,
        days: Annotated[int, Field(ge=1, le=183)] = 30,
    ) -> HistoryResult:
        """How one instance changed over the last N days: state transitions (running/stopped/terminated) and
        type changes, with timestamps. Requires the user to have history access."""
        body = await api_from(ctx).get(f"/v1/ec2/instances/{instance_id}/history", {"days": days})
        return HistoryResult(**body)

    @mcp.resource("inventory://schema", title="Inventory fields", mime_type="text/markdown")
    def schema() -> str:
        """What each field in the EC2 inventory means."""
        return (
            "# EC2 inventory fields\n"
            "- snapshot_ts: time (UTC) of the 2-hourly describe-instances snapshot\n"
            "- state: pending | running | stopping | stopped | shutting-down | terminated\n"
            "- vcpus / memory_gib / gpus: size of the instance type (from AWS specs)\n"
            "- lifecycle: on-demand or spot\n"
            "- team / service / name / cost_center: from the instance's tags\n"
            "- account_id: 12-digit AWS account; region / availability_zone: placement\n")

    @mcp.prompt(title="Investigate an instance")
    def investigate_instance(instance_id: str) -> str:
        """A ready-made prompt for a full report on one instance."""
        return (f"Investigate EC2 instance {instance_id}. Use get_instance, then instance_history for the last "
                "30 days. Report: what it is (name, team, service), where it runs, its size, its current state and "
                "how the state changed. Mention the snapshot time the data is from.")

    return mcp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp = build_server(args.transport)
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        # Stateless + JSON responses: any replica can answer any request, so it scales behind a plain load
        # balancer (the 2026-07-28 spec made this the default model). DNS-rebinding protection stays on.
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                             allowed_hosts=settings.allowed_hosts,
                                                             allowed_origins=[]))


if __name__ == "__main__":
    main()
