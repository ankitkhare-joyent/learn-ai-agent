# Lab 07 · The MCP server: JSON-RPC by hand

One sitting, about 40 minutes. Background: [docs/05](../docs/05-agents-and-mcp-basics.md),
[docs/06](../docs/06-mcp-servers.md). Code: [mcp_server/server.py](../mcp_server/server.py), `mcp_rpc` in
[lab/env.sh](env.sh).

An agent never calls the API. It calls MCP tools, and the MCP server calls the API for it. In this lab you are the
agent: you'll speak the protocol with curl. After it you can explain:

- how a client finds out where to get a token for an MCP server it has never seen
- what a tool definition is (the thing the model reads) and what a tool call looks like on the wire
- how the MCP server acts *for* the user without forwarding the user's token

Answers to every **Predict** are at the bottom.

## Before you start

```bash
source lab/env.sh
vim +/'def build_server' mcp_server/server.py
```

## 1. Knock without a token

**Predict (P1):** what does an MCP server return to a client with no token, and how does the client learn where to
log in?

```bash
curl -s -i -X POST "$MCP" -H 'Content-Type: application/json' -d '{}' | grep -iE '^HTTP|www-authenticate'
curl -s localhost:8001/.well-known/oauth-protected-resource/mcp | jq .
curl -s localhost:8000/.well-known/oauth-authorization-server | jq -c '{issuer, token_endpoint}'
```

## 2. List the tools

The server speaks MCP **2026-07-28**, the stateless revision: there is no `initialize` handshake and no session.
Every request carries the protocol version and the client's capabilities in `params._meta`, and repeats the method
(and tool name) in `Mcp-Method` / `Mcp-Name` headers so a load balancer can route it without parsing JSON. Look at
`mcp_rpc` in [env.sh](env.sh), then:

```bash
export MCP_TOKEN=$(api_token ec2-mcp)
MCP_VERBOSE=1 mcp_rpc tools/list 2>&1 | grep -E '^> (POST|Mcp-|MCP-)|^< HTTP'
mcp_rpc tools/list | jq -c '.result.tools[] | {name, title, readOnly: .annotations.readOnlyHint, outputSchema: (.outputSchema != null)}'
```

**Predict (P2):** all four tool definitions are sent to the model on *every* step of an agent loop. How big are they?

```bash
mcp_rpc tools/list | jq -c .result.tools | wc -c
mcp_rpc tools/list | jq -r '.result.tools[] | select(.name == "find_instances") | .description'
mcp_rpc tools/list | jq -c '.result.tools[] | select(.name == "find_instances") | .inputSchema.properties | {sort_by, account_id, limit}'
```

## 3. Call a tool

```bash
mcp_rpc tools/call '{"name":"find_instances","arguments":{"account_id":"392918273645","state":"running","sort_by":"vcpus","descending":true,"limit":2}}' > /tmp/find.json
jq -c '.result | {isError, content: [.content[].type], structured: (.structuredContent | keys)}' /tmp/find.json
jq -c '.result.structuredContent | {snapshot_ts, total_matches, returned, note}' /tmp/find.json
mcp_rpc resources/read '{"uri":"inventory://schema"}' | jq -r '.result.contents[0].text'
```

**Predict (P3):** the result comes back twice: as `structuredContent` and as text in `content`. Why both? And what
is `note` for, given that no human reads it?

## 4. Follow the call into the API

Each tool call logs the API request ID it used. Find yours in both logs:

```bash
RID=$(docker compose logs --since 2m mcp | grep 'api call path=/v1/ec2/instances ' | tail -1 | sed 's/.*request_id=//')
docker compose logs --since 2m mcp | grep "$RID"
docker compose logs --since 2m api | grep "$RID"
```

**Predict (P4):** your token's audience is `ec2-mcp`, and the API rejects that (Lab 06). So how many calls to
`/auth/token/exchange` will three tool calls with the same `MCP_TOKEN` cause?

```bash
sleep 1; S=$(date -u +%Y-%m-%dT%H:%M:%SZ); export MCP_TOKEN=$(api_token ec2-mcp)
for i in 1 2 3; do mcp_rpc tools/call '{"name":"fleet_summary","arguments":{}}' > /dev/null; done
sleep 1; docker compose logs --since "$S" api | grep -oE '"path": "[^"]+"' | grep -v healthz | sort | uniq -c
```

## Break it, then fix it

**Predict (P5):** each of these is a different kind of failure. Which ones are *protocol* errors (JSON-RPC `error`)
and which are *tool* errors (`result.isError: true`) the model gets to read?

```bash
# a) arguments that break the schema
mcp_rpc tools/call '{"name":"find_instances","arguments":{"account_id":"3929"}}' | jq -c '.error // {isError: .result.isError, text: .result.content[0].text[:120]}'
# b) a valid call for an instance that doesn't exist
mcp_rpc tools/call '{"name":"get_instance","arguments":{"instance_id":"i-0123456789abcdef0"}}' | jq -c '.error // {isError: .result.isError, text: .result.content[0].text}'
# c) a protocol version the server doesn't speak
MCP_VERSION=2030-01-01 mcp_rpc tools/list | jq -c .
# d) headers that disagree with the body
curl -s "$MCP" -H "Authorization: Bearer $MCP_TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fleet_summary","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}' | jq -c .
# e) a request that claims to be for another host (a DNS-rebinding attack looks like this)
curl -s -w ' %{http_code}\n' "$MCP" -H 'Host: evil.example:8001' -H "Authorization: Bearer $MCP_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
# f) an API token (aud=ec2-api) instead of an MCP token
MCP_TOKEN=$(api_token) mcp_rpc tools/list; echo
```

Now the one that matters most for agents. Create a read-only user (as in Lab 06) and ask for history through MCP:

```bash
AT=$(api_token ec2-api admin); RO_USER=lab-ro-$(date +%H%M%S); RO_PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' "$API/admin/users" \
  -d "{\"username\":\"$RO_USER\",\"password\":\"$RO_PW\",\"scopes\":[\"inventory:read\"]}"
RO_MCP=$(curl -s $API/auth/token -d username=$RO_USER --data-urlencode password=$RO_PW -d audience=ec2-mcp | jq -r .access_token)
MCP_TOKEN=$RO_MCP mcp_rpc tools/call '{"name":"instance_history","arguments":{"instance_id":"i-ff794db48de5443ab"}}' \
  | jq -r '.result.content[0].text'
```

**Predict (P6):** the API said `missing scope(s): inventory:history`. What does the model see, and which line of
`server.py` added the rest?

**Fix:** the fixes are all on the client side: correct arguments (a), a supported version (c), consistent headers
(d), the real host (e), the right audience (f). For the read-only user there's nothing to fix: the right outcome is
an agent that tells the user. You'll check that it does in Lab 08 (keep `RO_USER` and `RO_PW`, or create another).

## Check your predictions

**P1.** `HTTP/1.1 401 Unauthorized` with
`www-authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://localhost:8001/.well-known/oauth-protected-resource/mcp"`.
That URL (RFC 9728) says `"authorization_servers":["http://localhost:8000"]` and `"scopes_supported":["inventory:read"]`,
and the authorization server's metadata (RFC 8414) gives the token endpoint. A client like Claude Code follows that
chain by itself; that's how "add this MCP server URL" can end in a browser login.

**P2.** The verbose run shows `> MCP-Protocol-Version: 2026-07-28`, `> Mcp-Method: tools/list` and `< HTTP/1.1 200 OK`.
All four tools are `readOnly: true` with an output schema. The definitions are about 8,700 bytes of JSON (roughly
2,000+ tokens), mostly the output schemas. That's why the agents cache the prompt (Lab 08 shows `cache_read`). The
`find_instances` description tells the model to use `sort_by="vcpus"` with `descending=true` for "largest", and the
schema makes it impossible to send anything else: `sort_by` is an `enum`, `account_id` has a 12-digit `pattern`,
`limit` is capped at 50.

**P3.** `isError: false`, content type `text`, structured keys `instances, note, returned, snapshot_ts,
total_matches`. `structuredContent` is for programs (it matches the `outputSchema`); the text copy is for clients
that only pass text to the model. `note` is `Showing 2 of 284 matches; narrow the filters or raise limit (max 50).`:
it's written for the *model*, so it knows the list is partial and what to do about it.

**P4.** One. The log shows `1 "path": "/auth/token/exchange"` and `3 "path": "/v1/ec2/summary"` (plus one
`/auth/token` from `api_token`). The MCP server swaps your `ec2-mcp` token for an `ec2-api` token once (RFC 8693),
caches it until a minute before it expires, and never forwards your token to the API. The exchanged token carries
`act: {sub: ec2-mcp}`, so the API knows both who the user is and who is acting for them. The same request ID shows
up in the `ec2-mcp api call ... request_id=...` line and the API's JSON log line.

**P5.** Tool errors (the model reads them and can correct itself): (a) `isError: true`,
`Error executing tool find_instances: 1 validation error ... String should match pattern '^\d{12}$'` (the server
checked the schema before calling the API, so the API never saw it); (b) `isError: true`,
`not found: instance not found in the last 30 days` (the API was called and said 404; `get()` turned that into a
sentence). Protocol errors (the client library deals with them, the model never sees them): (c)
`{"code":-32022,"message":"Unsupported protocol version","data":{"supported":["2026-07-28"],"requested":"2030-01-01"}}`;
(d) `{"code":-32020,"message":"mcp-method header does not match the request body's method"}`. Transport and auth
errors stop before JSON-RPC: (e) `Invalid Host header 421` (DNS-rebinding protection is on); (f)
`{"error": "invalid_token", "error_description": "Authentication required"}` with a 401, because the token's
audience is `ec2-api`.

**P6.** `Error executing tool instance_history: permission denied: missing scope(s): inventory:history. The user lacks
this permission; tell them instead of retrying.` The second sentence comes from the `403` branch of
`InventoryApi.get()`. Errors are a prompt: this one stops a model from looping on a call that can never succeed.

## What to remember

- MCP over HTTP is JSON-RPC plus OAuth discovery. Since 2026-07-28 every request stands alone, so any replica can
  serve it.
- Tool definitions are prompt text you pay for on every step: keep them few, precise and constrained.
- Validation errors and "not found" are for the model; protocol and auth errors are for the client. Design both.

Next: [Lab 08 · The agents](08-agents.md).
