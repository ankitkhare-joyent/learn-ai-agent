# Hands-on lab: the stack, one component per sitting

The lessons in [docs/](../docs) explain the design. This track has you poke each running component until you could
debug it on your own. It follows the data: secrets first (everything boots from them), then storage, the catalog,
writes, the query engine, the API, the MCP server, and finally the agents on top.

Every lab has the same shape:

1. **Look**: real commands against your running stack, inspecting what the component stores, logs and sends.
2. **Predict** before you run: each step asks a question (P1, P2, …). Write your guess down first; the answers, with
   the output I got when I ran every command, are at the bottom of each file.
3. **Break it, then fix it**: at least one failure on purpose, so you've seen what it looks like before it happens
   for real.

## How to use it

```bash
cd ~/workstuff/learn-ai-agent
docker ps --format '{{.Names}}\t{{.Status}}' | grep ec2-lakehouse    # the stack must be up (mise run up)
source lab/env.sh                                                     # once per terminal
glow lab/01-openbao.md                                                # or: vim lab/01-openbao.md
```

A tmux split works well: the lab on one side, a shell on the other, and a third pane for `docker compose logs -f`
when a lab asks for it.

| Lab | Component | You'll be able to explain | Changes anything? |
|---|---|---|---|
| [01](01-openbao.md) | OpenBao | how a secret reaches a process without touching config; why a missing secret surfaces 3 hops later | no |
| [02](02-s3-storage.md) | S3 (SeaweedFS) | raw vs Iceberg files, why one day is 27× smaller, what a signed S3 request is | no |
| [03](03-iceberg-catalog.md) | Iceberg catalog (Lakekeeper + Postgres) | table → metadata → snapshot → manifests → files; why server-vended config wins | no |
| [04](04-etl-commits.md) | The ETL write path | what a commit does; how the API notices; repairing a bad append | yes: re-runs one day, breaks it, repairs it |
| [05](05-duckdb-engine.md) | DuckDB | what a query reads from S3; partition pruning; why only *now* is cached | no |
| [06](06-rest-api.md) | REST API | JWT claims, audience vs scope, 401/403/422, following a request ID | creates a disabled test user |
| [07](07-mcp-server.md) | MCP server | OAuth discovery, tools on the wire, token exchange, tool vs protocol errors | creates a test user |
| [08](08-agents.md) | Agents | every hop of a question; step budgets; how a permission error reaches the user | calls Claude (cents) |

Each lab is one sitting of 30-45 minutes. Do them in order: later labs reuse things you saw earlier.

## Helpers

| | What | Where |
|---|---|---|
| `source lab/env.sh` | `$API`, `$MCP`, `$CAT`, `$BAO_TOKEN` and the functions below | [env.sh](env.sh) |
| `bao_keys PATH` | a secret's field names and version, never the values | |
| `bao_field PATH FIELD` | one secret value, for use inside `$( … )` only | |
| `s3curl KEY [curl args]` | a SigV4-signed S3 request (`=` in keys written as `%3D`) | |
| `cat_prefix`, `table_state` | the warehouse ID; the table's current snapshot and last three commits | |
| `api_token [aud] [user]` | an access token (`ec2-api` or `ec2-mcp`; `agent` or `admin`) | |
| `jwt_claims TOKEN` | a token's decoded payload | |
| `mcp_rpc METHOD [PARAMS]` | one MCP JSON-RPC request with `$MCP_TOKEN` | |
| `mise run -q lab:duck -- "SQL" …` | DuckDB on your laptop with S3 and the catalog attached as `lake` | [duck.py](duck.py) |
| `mise run -q lab:scan` | PyIceberg reading the table from your laptop (Lab 03) | [iceberg_scan.py](iceberg_scan.py) |
| `mise run -q lab:oops` | appends duplicate rows on purpose (Lab 04) | [oops_append.py](oops_append.py) |

None of the labs print a secret value. The ones that need a password or key read it from OpenBao into a shell
variable or a header.

## If something is off

- `lab/env.sh: no .bao/dev-token`: run it from the repo root, after `mise run up`.
- Everything from OpenBao comes back empty after a Docker restart: OpenBao starts sealed. `mise run unseal`.
- `readyz` says `"ready": false`: the API hasn't loaded the hot cache yet; `docker compose logs api | tail`.
- A lab's numbers don't match the answers exactly: snapshot IDs, timings, file counts and token counts move every
  time someone commits or runs an agent. The shape and the reasoning should match. Anything else is worth a look.

## The stack, with the lab that covers each part

```
                        you (terminal)
                              │  question
                              ▼
   ┌──────────────────────────────────────────────┐        Claude API
   │ 08  agents   raw loop · LangGraph · Strands  │◄──────► claude-opus-5-5
   └──────────────────────┬───────────────────────┘        (tools in, tool_use out)
                          │ MCP JSON-RPC over HTTP, Bearer aud=ec2-mcp
                          ▼
   ┌──────────────────────────────────────────────┐
   │ 07  MCP server :8001   4 read-only tools     │
   └──────────────────────┬───────────────────────┘
                          │ token exchange, then GET /v1/ec2/*  Bearer aud=ec2-api
                          ▼
   ┌──────────────────────────────────────────────┐
   │ 06  REST API :8000   auth · scopes · logs    │
   │ 05  DuckDB           hot cache │ history     │
   └─────────┬──────────────────────┬─────────────┘
   snapshot  │ every 60 s           │ read pruned day files
   id        ▼                      ▼
   ┌───────────────────────┐   ┌────────────────────────────────┐
   │ 03  Lakekeeper :8181  │──►│ 02  SeaweedFS S3 :9000         │
   │     + Postgres        │   │     raw/  (8,784 nested files) │
   │ one pointer per table │   │     lakehouse/ (Iceberg)       │
   └───────────▲───────────┘   └───────────────▲────────────────┘
               │ commit (pointer swap)         │ write Parquet + metadata
   ┌───────────┴───────────────────────────────┴────────────────┐
   │ 04  ETL   pipeline/load_iceberg.py (overwrite one day)     │
   └────────────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────────────┐
   │ 01  OpenBao :8200   AppRole per service → env at start;    │
   │     dev token → your laptop (agents, labs)                 │
   └────────────────────────────────────────────────────────────┘
        feeds every box above: S3 keys, JWT key, client secret, passwords
```
