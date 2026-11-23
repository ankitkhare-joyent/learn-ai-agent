# 06 · Building production MCP servers

Code: [mcp_server/server.py](../mcp_server/server.py) (MCP Python SDK **v2.3**, spec **2026-07-28**).
Tests: [tests/test_mcp.py](../tests/test_mcp.py).

## 1. What changed in 2026 (so older tutorials don't mislead you)

| Change (spec 2026-07-28, SDK v2) | What it means for you |
|---|---|
| **Stateless protocol.** No `initialize` handshake or `Mcp-Session-Id` required; every request carries version/capabilities in `_meta`; discovery is `server/discover`. | Run N replicas behind a round-robin load balancer, no sticky sessions, no shared session store. This server runs `stateless_http=True, json_response=True`. |
| **`Mcp-Method` / `Mcp-Name` HTTP headers** on Streamable HTTP. | Gateways/WAFs can route, rate-limit and meter per tool without parsing JSON bodies. |
| **Cacheable lists** (`ttlMs`, `cacheScope` on tools/list etc.). | Clients stop re-fetching tool lists on every turn. |
| **Multi round-trip requests** replace held-open streams for elicitation/sampling. | Asking the user a question mid-tool no longer needs a long-lived SSE stream. |
| **Auth hardening**: RFC 9207 issuer checks, Client ID Metadata Documents replace Dynamic Client Registration, credentials bound to their issuer. | Use your IdP; don't build an authorization server into the MCP server. |
| **Deprecated**: Roots, Sampling, Logging (12-month window); the old HTTP+SSE transport. | Use Streamable HTTP. |
| **Python SDK v2**: `FastMCP` → `MCPServer`, one `Client` object; serves 2025-era *and* 2026 clients from the same app. | That's why the agents here (frameworks still on the v1 client, `mcp<2`) talk to this v2 server without changes — the server negotiated `2025-11-25` with them. |

## 2. Tool design: the part that decides agent quality

The model only sees your tool names, descriptions and schemas. **Descriptions are prompts.**

1. **Few, task-shaped tools.** The REST API has 7 inventory/admin routes; the MCP server exposes 4 tools. No admin tools
   at all — an agent doesn't need to create users.
2. **Say when to use it and how**, in the docstring: *"For 'largest' use sort_by='vcpus' with descending=true."*
   This one sentence is the difference between the model sorting correctly and it fetching 500 rows to sort itself.
3. **Constrain inputs**: `Literal[...]` for enums, regex patterns (`^i-[0-9a-f]{8,17}$`, `^\d{12}$`), numeric bounds
   (`limit` 1-50). Bad arguments fail validation with a message the model can fix, before touching your API.
4. **Compact, structured outputs** (`outputSchema` from return type annotations). Return the 16 fields that matter, not
   the 60-field DescribeInstances blob. Every byte goes into the context window and costs tokens on *every later turn*.
5. **Bound results and say so**: `total_matches`, `returned`, and a `note` ("showing 10 of 284; narrow the filters").
6. **Actionable errors** (`ToolError` → `isError: true`): "not found", "permission denied — tell the user instead of
   retrying", "account_id must be 12 digits". Never leak stack traces or internal hostnames.
7. **Annotations**: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`. Hosts use them to decide what
   needs user confirmation.
8. **Include provenance** in results (snapshot time), and instruct the model to report it.

## 3. Security model

```
user ──login (IdP)──► token A (aud = ec2-mcp)
agent host ──Bearer A──► MCP server  (resource server: validates A's signature via JWKS, issuer, audience, scopes)
MCP server ──client creds + A──► API /auth/token/exchange (RFC 8693) ──► token B (aud = ec2-api, same user, act = ec2-mcp)
MCP server ──Bearer B──► REST API (enforces the user's own scopes)
```

Why each piece:
- **Audience binding**: a token minted for the MCP server is rejected by the API and vice versa (both tested). Stops a
  stolen token being replayed against a different service.
- **No token passthrough**: forwarding token A to the API is explicitly forbidden by the MCP spec (confused-deputy risk,
  broken audit). Token exchange keeps the user's identity *and* records the MCP server as the actor (`act` claim).
- **Protected Resource Metadata** (`/.well-known/oauth-protected-resource/mcp`, RFC 9728): a 401 points clients to the
  authorization server, so MCP clients can discover how to log in.
- **Least privilege end to end**: a user with only `inventory:read` calling `instance_history` gets "permission denied"
  through MCP (tested in `test_user_permissions_flow_through_mcp`).
- **stdio mode** takes credentials from the environment and does no OAuth, as the spec requires.
- **DNS-rebinding protection** (`allowed_hosts`) is on for the HTTP transport.
- **Treat tool output as untrusted** on the agent side: data from your API can contain text that looks like
  instructions (an instance `Name` tag saying "ignore previous instructions…"). Keep tools read-only, keep write tools
  behind approval, and never let tool output choose new credentials or URLs.

Common production findings (2026 security reviews): command/shell injection from tool arguments (~43% of findings),
omnibus "run any query" tools, secrets in MCP config files, over-broad tokens. None apply here by construction.

## 4. Operating it

- **Deploy** as a normal stateless HTTP service (Deployment + HPA, or Lambda/AgentCore Runtime). Health checks, resource
  limits, JSON logs with the request ID that is forwarded to the API (`x-request-id`).
- **Gateway** in front when you have several MCP servers: one place for authN, rate limits, tool allow-lists, audit.
  On AWS, **AgentCore Gateway** can also *generate* MCP tools from an OpenAPI spec — a fast start, but you lose the
  task-shaped design above unless you curate the spec.
- **Version tools carefully**: renaming a tool or changing semantics breaks prompts and evals. Add new tools, deprecate
  old ones.
- **Evaluate**: keep a set of questions → expected tool calls (see [08-production-agents.md](08-production-agents.md)).

## 5. Try it

```bash
mise run up
# list tools as a client would (needs a token whose audience is the MCP server)
python -c "from agents.common import mcp_access_token; print(mcp_access_token())"
# or point Claude Desktop / Claude Code at it in stdio mode (credentials from env):
#   MCP_API_BASE_URL=http://localhost:8000 MCP_API_USERNAME=agent MCP_API_PASSWORD=$(...) \
#   uv run --project mcp_server python mcp_server/server.py --transport stdio
```

Exercise: add a `compare_snapshots(account_id, hours_ago)` tool that returns instances launched/terminated since then.
Write its description first, then the code, then a test.

### Sources
- [The 2026-07-28 MCP specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/) · [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [MCP Python SDK v2](https://pypi.org/project/mcp/) · [Pydantic: MCP Python SDK v2](https://pydantic.dev/articles/mcp-python-sdk-v2-beta)
- [MCP server security best practices 2026](https://www.digitalapplied.com/blog/mcp-server-security-best-practices-2026-engineering-guide) · [MCP best practices](https://mcp-best-practice.github.io/mcp-best-practice/best-practice/)
- [AgentCore Gateway: REST/OpenAPI to MCP](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-core-concepts.html)
- RFC 8693 (token exchange), RFC 8707 (resource indicators), RFC 9728 (protected resource metadata)
