# 03 · The REST API: design, auth, and production notes

Code: [api/app/](../api/app) · Swagger UI: http://localhost:8000/docs (log in with `admin` / `mise run admin-password`).

## Endpoints

| Route | Scope | What |
|---|---|---|
| `POST /auth/token` | — | OAuth2 password grant → JWT. Form field `audience` = `ec2-api` (default) or `ec2-mcp` |
| `POST /auth/token/exchange` | MCP client auth | RFC 8693: swap an `ec2-mcp` token for an `ec2-api` token (MCP server only) |
| `GET /.well-known/jwks.json` | — | public keys that verify our tokens |
| `GET /.well-known/oauth-authorization-server` | — | RFC 8414 metadata (token endpoint, JWKS, scopes) |
| `GET /auth/me` | any | who am I, scopes, acting party |
| `POST/GET /admin/users`, `POST /admin/users/{u}/disable` | `users:admin` | user management |
| `GET /v1/ec2/instances` | `inventory:read` (+`inventory:history` with `snapshot_ts`) | filter / sort / paginate one snapshot |
| `GET /v1/ec2/instances/{id}` | `inventory:read` (history fallback needs `inventory:history`) | one instance; finds terminated ones in history |
| `GET /v1/ec2/instances/{id}/history` | `inventory:history` | state/type changes over N days |
| `GET /v1/ec2/summary` | `inventory:read` | counts + vCPU/memory/GPU totals grouped by a dimension |
| `GET /v1/ec2/snapshots` | `inventory:history` | available snapshot timestamps |
| `/healthz`, `/readyz` | — | liveness; readiness = hot cache loaded |

## Design choices worth copying

- **Two read paths** ([engine.py](../api/app/engine.py)): the latest snapshot lives in DuckDB memory and is swapped
  atomically when the Iceberg table's snapshot ID changes (checked every 60 s from catalog metadata, no data read);
  history queries go to Iceberg through DuckDB's `iceberg` extension with time filters so only the needed day
  partitions are read. Measurements in [04](04-engine-choice.md).
- **Parameterized SQL only**; sort/group columns come from enums; `limit` ≤ 500; DuckDB `memory_limit`/`threads` set.
- **Per-request DuckDB cursors** (one connection is not thread-safe; cursors are cheap).
- **Response models with descriptions** → the OpenAPI spec is precise enough to generate clients or MCP tools.
- **Structured JSON logs** with a request ID that callers (the MCP server) can pass in and get back.
- **Idempotent bootstrap**: admin and agent users are upserted from OpenBao secrets at start, so rotating a password in
  OpenBao + restarting is the whole rotation procedure.

## Authentication and authorization, piece by piece

| Concept | Here | Why it matters |
|---|---|---|
| Password hashing | **Argon2id** via `pwdlib` | slow, memory-hard hashing; a constant-time dummy check for unknown users prevents user enumeration |
| Access tokens | **JWT, RS256**, 30-min TTL, `iss`/`sub`/`aud`/`scope`/`jti`/`exp`/`nbf` | short-lived, verifiable without a DB call |
| Signing key | RSA key from **OpenBao**; public half at `/.well-known/jwks.json` with a `kid` | other services verify tokens with no shared secret; `kid` enables key rotation |
| Audience | `ec2-api` vs `ec2-mcp` | a token for one service can't be replayed at another |
| Scopes | `inventory:read`, `inventory:history`, `users:admin` | coarse permissions checked per route (`Security(..., scopes=[...])`); a token never gets more than the user has |
| Disable takes effect immediately | user status checked on every request | JWTs can't be revoked otherwise until they expire |
| Delegation | **token exchange** with an `act` claim | the MCP server acts *for* the user with the user's permissions, and logs show both |

**Production**: don't run your own user store. Use your IdP (Okta / Entra ID / Cognito / Keycloak) for login, MFA and
groups; the API only *validates* tokens against the IdP's JWKS and maps groups/claims to scopes. Row-level rules (e.g.
"team X sees only its accounts") belong in the query layer: add `account_id IN (:allowed)` from the token's claims.

## Ray Serve vs a plain Deployment

A common production setup runs the API on RayService. Ray Serve is valuable for model serving, GPU scheduling and composing
deployments. For a stateless FastAPI over DuckDB it adds an extra hop and operational surface; a Kubernetes
Deployment with HPA (CPU / RPS via KEDA) is simpler. If you stay on Ray, keep one replica per pod, set
`max_ongoing_requests`, and remember each replica holds its own hot cache.

## Exercises

1. Add row-level security: an `accounts` claim in the token, enforced in `_filters`.
2. Add cursor-based pagination (`after=<instance_id>`) and compare with offset on page 50.
3. Add an OpenTelemetry middleware and view a trace that spans agent → MCP → API → DuckDB.
