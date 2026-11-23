"""EC2 inventory REST API over Iceberg.

Run locally:  docker compose up -d api   ->  http://localhost:8000/docs
"""
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Path, Query, Request, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import security as sec
from .engine import COLUMNS, Engine
from .models import (
    GroupBy,
    Instance,
    InstanceHistory,
    InstanceLookup,
    InstancePage,
    NewUser,
    SortBy,
    State,
    StateChange,
    Summary,
    SummaryRow,
    Token,
    UserOut,
)
from .settings import get_settings


# ------------------------------------------------------------------ logging: one JSON line per event
class JsonFormatter(logging.Formatter):
    def format(self, record):
        base = {"ts": datetime.now(UTC).isoformat(), "level": record.levelname, "logger": record.name,
                "msg": record.getMessage()}
        base.update({k: v for k, v in record.__dict__.items() if k not in logging.LogRecord(
            "", 0, "", 0, "", (), None).__dict__ and k not in ("message", "asctime")})
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("api")


# ------------------------------------------------------------------ app lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    sec.init_user_store()
    engine = Engine(settings)
    try:
        engine.refresh(force=True)
    except Exception:
        log.exception("initial cache load failed; /readyz stays false until the background refresh succeeds")
    engine.start_background_refresh()
    app.state.engine = engine
    yield
    engine.stop()


app = FastAPI(
    title="EC2 Inventory API",
    version="1.0.0",
    description="Read-only EC2 `describe-instances` inventory, snapshotted every 2 hours into Apache Iceberg.\n\n"
                "Authenticate with `POST /auth/token` (OAuth2 password grant), then send `Authorization: Bearer`.",
    lifespan=lifespan,
)


def engine(request: Request) -> Engine:
    return request.app.state.engine


EngineDep = Annotated[Engine, Depends(engine)]
Read = Security(sec.current_principal, scopes=["inventory:read"])
History = Security(sec.current_principal, scopes=["inventory:read", "inventory:history"])
Admin = Security(sec.current_principal, scopes=["users:admin"])


@app.middleware("http")
async def access_log(request: Request, call_next):
    """Request ID + structured access log. The request ID is returned so callers (and the MCP server) can
    correlate a tool call with the API log line."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    log.info("request", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                               "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return response


# ------------------------------------------------------------------ health
@app.get("/healthz", tags=["ops"], include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"], include_in_schema=False)
def readyz(eng: EngineDep):
    c = eng.cache
    body = {"ready": eng.ready, "snapshot_ts": c.snapshot_ts, "rows": c.rows, "iceberg_snapshot_id": c.iceberg_snapshot_id}
    return JSONResponse(json.loads(json.dumps(body, default=str)), status_code=200 if eng.ready else 503)


# ------------------------------------------------------------------ auth
@app.get("/.well-known/jwks.json", tags=["auth"])
def jwks():
    """Public signing keys. Resource servers (the MCP server) verify our tokens with these - no shared secret."""
    return sec.jwks()


@app.get("/.well-known/oauth-authorization-server", tags=["auth"])
def oauth_metadata():
    """RFC 8414 metadata: how MCP clients discover where to get tokens."""
    base = get_settings().public_url
    return {"issuer": get_settings().jwt_issuer, "token_endpoint": f"{base}/auth/token",
            "jwks_uri": f"{base}/.well-known/jwks.json", "scopes_supported": list(sec.SCOPES),
            "grant_types_supported": ["password", "urn:ietf:params:oauth:grant-type:token-exchange"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"]}


@app.post("/auth/token", response_model=Token, tags=["auth"])
def login(username: Annotated[str, Form()], password: Annotated[str, Form()],
          scope: Annotated[str, Form(description="Space-separated scopes; empty = all the user has")] = "",
          audience: Annotated[str, Form(description="ec2-api (this API) or ec2-mcp (the MCP server)")] = sec.API_AUDIENCE):
    """OAuth2 password grant (for learning; production uses your IdP's authorization-code + PKCE flow)."""
    user = sec.authenticate(username, password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    if audience not in sec.AUDIENCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_target")
    granted = set(user["scopes"].split())
    requested = set(scope.split()) or granted
    scopes = sorted(requested & granted)  # never more than the user has
    token, ttl = sec.issue_token(username, scopes, audience)
    return Token(access_token=token, expires_in=ttl, scope=" ".join(scopes))


_basic = HTTPBasic()
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


@app.post("/auth/token/exchange", response_model=Token, tags=["auth"])
def token_exchange(client: Annotated[HTTPBasicCredentials, Depends(_basic)],
                   grant_type: Annotated[str, Form()], subject_token: Annotated[str, Form()],
                   subject_token_type: Annotated[str, Form()] = ACCESS_TOKEN_TYPE,
                   audience: Annotated[str, Form()] = sec.API_AUDIENCE,
                   scope: Annotated[str, Form()] = ""):
    """RFC 8693 token exchange, for the MCP server only.

    The MCP server receives a user token whose audience is *the MCP server*. It may not forward that token
    (token passthrough). Instead it authenticates as itself and exchanges it for a short-lived token for
    this API, scoped to what the user has - so per-user authorization still applies end to end.
    """
    s = get_settings()
    ok_id = secrets.compare_digest(client.username, s.mcp_client_id)
    ok_secret = secrets.compare_digest(client.password, s.mcp_client_secret.get_secret_value())
    if not (ok_id and ok_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_client", headers={"WWW-Authenticate": "Basic"})
    if grant_type != TOKEN_EXCHANGE or subject_token_type != ACCESS_TOKEN_TYPE or audience != sec.API_AUDIENCE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported_grant_type or invalid_target")
    claims = sec.Principal(sec.decode_token(subject_token, sec.MCP_AUDIENCE))
    user = sec.get_user(claims.username)
    if user is None or user["disabled"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_grant")
    allowed = claims.scopes & set(user["scopes"].split())
    scopes = sorted((set(scope.split()) & allowed) if scope else allowed)
    token, ttl = sec.issue_token(claims.username, scopes, sec.API_AUDIENCE, actor=client.username)
    return Token(access_token=token, expires_in=ttl, scope=" ".join(scopes), issued_token_type=ACCESS_TOKEN_TYPE)


@app.get("/auth/me", tags=["auth"])
def me(p: Annotated[sec.Principal, Security(sec.current_principal)]):
    return {"username": p.username, "scopes": sorted(p.scopes), "acting_party": p.get("act")}


# ------------------------------------------------------------------ admin
@app.post("/admin/users", status_code=201, response_model=UserOut, tags=["admin"])
def create_user(body: NewUser, _: Annotated[sec.Principal, Admin]):
    sec.create_user(body.username, body.password, body.scopes)
    return next(u for u in sec.list_users() if u["username"] == body.username)


@app.get("/admin/users", response_model=list[UserOut], tags=["admin"])
def users(_: Annotated[sec.Principal, Admin]):
    return sec.list_users()


@app.post("/admin/users/{username}/disable", status_code=204, tags=["admin"])
def disable_user(username: str, _: Annotated[sec.Principal, Admin]):
    sec.set_disabled(username, True)


# ------------------------------------------------------------------ inventory
def _filters(account_id, region, state, instance_type, instance_family, team, service, min_vcpus, name_contains):
    """Build a parameterized WHERE clause. Values are always bind parameters, never string-formatted SQL."""
    where, params = [], []
    for col, val in (("account_id", account_id), ("region", region), ("instance_type", instance_type),
                     ("instance_family", instance_family), ("team", team), ("service", service)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if state:
        where.append(f"state IN ({', '.join('?' * len(state))})")
        params.extend(s.value for s in state)
    if min_vcpus is not None:
        where.append("vcpus >= ?")
        params.append(min_vcpus)
    if name_contains:
        where.append("name ILIKE ?")
        params.append(f"%{name_contains}%")
    return where, params


@app.get("/v1/ec2/instances", response_model=InstancePage, tags=["inventory"])
def list_instances(
    eng: EngineDep, p: Annotated[sec.Principal, Read],
    account_id: Annotated[str | None, Query(pattern=r"^\d{12}$", description="12-digit AWS account ID")] = None,
    region: Annotated[str | None, Query(description="e.g. us-east-1")] = None,
    state: Annotated[list[State] | None, Query(description="Repeat for several states")] = None,
    instance_type: Annotated[str | None, Query(description="e.g. m7i.large")] = None,
    instance_family: Annotated[str | None, Query(description="e.g. m7i")] = None,
    team: str | None = None,
    service: str | None = None,
    name_contains: Annotated[str | None, Query(max_length=64)] = None,
    min_vcpus: Annotated[int | None, Query(ge=1)] = None,
    snapshot_ts: Annotated[datetime | None, Query(description="Historical snapshot (needs inventory:history); "
                                                              "omit for the latest")] = None,
    sort_by: SortBy = SortBy.instance_id,
    descending: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    """Instances in one inventory snapshot (the latest by default), filtered, sorted and paginated."""
    where, params = _filters(account_id, region, state, instance_type, instance_family, team, service, min_vcpus,
                             name_contains)
    if snapshot_ts is None:
        source, snap = "latest", eng.cache.snapshot_ts
    else:
        if "inventory:history" not in p.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing scope(s): inventory:history")
        source, snap = eng.lake_table, snapshot_ts
        where.append("snapshot_ts = ?")
        params.append(snapshot_ts)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = f"{sort_by.value} {'DESC' if descending else 'ASC'} NULLS LAST, instance_id"
    rows = eng.query(f"""SELECT {COLUMNS}, count(*) OVER () AS _total FROM {source} {clause}
                         ORDER BY {order} LIMIT ? OFFSET ?""", [*params, limit, offset])
    total = rows[0]["_total"] if rows else 0
    return InstancePage(snapshot_ts=snap, total=total, limit=limit, offset=offset,
                        next_offset=offset + limit if offset + limit < total else None,
                        items=[Instance(**r) for r in rows])


@app.get("/v1/ec2/instances/{instance_id}", response_model=InstanceLookup, tags=["inventory"])
def get_instance(eng: EngineDep, p: Annotated[sec.Principal, Read],
                 instance_id: Annotated[str, Path(pattern=r"^i-[0-9a-f]{8,17}$")],
                 lookback_days: Annotated[int, Query(ge=1, le=366, description="How far back to search if the "
                                                     "instance is not in the latest snapshot")] = 30):
    """One instance by ID. Falls back to history (needs inventory:history) if it no longer exists."""
    rows = eng.query(f"SELECT {COLUMNS} FROM latest WHERE instance_id = ?", [instance_id])
    if rows:
        return InstanceLookup(instance=Instance(**rows[0]), in_latest_snapshot=True,
                              latest_snapshot_ts=eng.cache.snapshot_ts)
    if "inventory:history" not in p.scopes:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "not in the latest snapshot; searching history needs the inventory:history scope")
    since = eng.cache.snapshot_ts - timedelta(days=lookback_days)
    rows = eng.query(f"""SELECT {COLUMNS} FROM {eng.lake_table} WHERE instance_id = ? AND snapshot_ts >= ?
                         ORDER BY snapshot_ts DESC LIMIT 1""", [instance_id, since])
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"instance not found in the last {lookback_days} days")
    return InstanceLookup(instance=Instance(**rows[0]), in_latest_snapshot=False,
                          latest_snapshot_ts=eng.cache.snapshot_ts)


@app.get("/v1/ec2/instances/{instance_id}/history", response_model=InstanceHistory, tags=["inventory"])
def instance_history(eng: EngineDep, _: Annotated[sec.Principal, History],
                     instance_id: Annotated[str, Path(pattern=r"^i-[0-9a-f]{8,17}$")],
                     days: Annotated[int, Query(ge=1, le=366)] = 30):
    """State/type changes of one instance over the last N days (unchanged snapshots are collapsed)."""
    since = eng.cache.snapshot_ts - timedelta(days=days)
    rows = eng.query(f"""
        WITH h AS (
            SELECT snapshot_ts, state, instance_type, state_reason,
                   lag(state) OVER w AS prev_state, lag(instance_type) OVER w AS prev_type
            FROM {eng.lake_table} WHERE instance_id = ? AND snapshot_ts >= ?
            WINDOW w AS (ORDER BY snapshot_ts))
        SELECT *, min(snapshot_ts) OVER () AS first_seen, max(snapshot_ts) OVER () AS last_seen FROM h
        ORDER BY snapshot_ts""", [instance_id, since])
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"instance not seen in the last {days} days")
    changes = [StateChange(**{k: r[k] for k in ("snapshot_ts", "state", "instance_type", "state_reason")})
               for r in rows if r["state"] != r["prev_state"] or r["instance_type"] != r["prev_type"]]
    return InstanceHistory(instance_id=instance_id, first_seen=rows[0]["first_seen"],
                           last_seen=rows[0]["last_seen"], changes=changes)


@app.get("/v1/ec2/summary", response_model=Summary, tags=["inventory"])
def summary(eng: EngineDep, _: Annotated[sec.Principal, Read], group_by: GroupBy = GroupBy.region,
            account_id: Annotated[str | None, Query(pattern=r"^\d{12}$")] = None,
            region: str | None = None,
            state: Annotated[list[State] | None, Query()] = None):
    """Counts and capacity (vCPUs, memory, GPUs) of the latest snapshot, grouped by one dimension."""
    where, params = _filters(account_id, region, state, None, None, None, None, None, None)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = eng.query(f"""SELECT {group_by.value} AS key, count(*) AS instances, coalesce(sum(vcpus), 0) AS vcpus,
                                coalesce(sum(memory_gib), 0) AS memory_gib, coalesce(sum(gpus), 0) AS gpus
                         FROM latest {clause} GROUP BY 1 ORDER BY instances DESC""", params)
    return Summary(snapshot_ts=eng.cache.snapshot_ts, group_by=group_by, rows=[SummaryRow(**r) for r in rows])


@app.get("/v1/ec2/snapshots", tags=["inventory"])
def snapshots(eng: EngineDep, _: Annotated[sec.Principal, History],
              days: Annotated[int, Query(ge=1, le=31)] = 1):
    """Snapshot timestamps available in the last N days (for use as `snapshot_ts`)."""
    since = eng.cache.snapshot_ts - timedelta(days=days)
    rows = eng.query(f"""SELECT snapshot_ts, count(*) AS instances FROM {eng.lake_table}
                         WHERE snapshot_ts > ? GROUP BY 1 ORDER BY 1 DESC""", [since])
    return {"snapshots": rows}
