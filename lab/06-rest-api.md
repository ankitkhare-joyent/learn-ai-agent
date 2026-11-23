# Lab 06 · The REST API: tokens, scopes and the two read paths

One sitting, about 40 minutes. Background: [docs/03](../docs/03-rest-api.md). Code:
[api/app/main.py](../api/app/main.py), [api/app/security.py](../api/app/security.py).

The API is the only thing that touches the lakehouse on behalf of users and agents. After this lab you can explain:

- what is inside an access token and how another service verifies it without a shared secret
- the difference between a 401, a 403 and a 422 here, and which one an agent can fix by itself
- how to follow one request through the logs

Answers to every **Predict** are at the bottom. Timings, token IDs and timestamps will differ on your run.

## Before you start

```bash
source lab/env.sh
curl -s $API/healthz; echo
curl -s $API/readyz | jq -c .
```

`healthz` says the process is alive; `readyz` says the hot cache is loaded. Kubernetes restarts a pod that fails
the first and stops sending traffic to one that fails the second.

## 1. How a client discovers where to log in

```bash
curl -s $API/.well-known/oauth-authorization-server | jq .
curl -s $API/.well-known/jwks.json | jq '.keys[] | {kty, alg, use, kid, n: (.n[:16] + "...")}'
```

## 2. Get a token and take it apart

`api_token` logs in as the `agent` user with the password from OpenBao (password grant, local only).

**Predict (P1):** which claims will the token carry, and how long is it valid? Which claim stops someone from using
it at the MCP server?

```bash
TOKEN=$(api_token)
jwt_claims "$TOKEN"
jwt_claims "$TOKEN" | jq '.exp - .iat'
python3 -c 'import base64, sys; h = sys.argv[1].split(".")[0]; print(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4)).decode())' "$TOKEN"
curl -s -H "Authorization: Bearer $TOKEN" $API/auth/me | jq -c .
```

Compare the token header's `kid` with the JWKS `kid` from step 1.

## 3. Two read paths, timed

**Predict (P2):** rank these by latency: one instance (hot cache), its 30-day history and its 180-day history
(both read Iceberg).

```bash
I=i-ff794db48de5443ab
for p in "v1/ec2/instances/$I" "v1/ec2/instances/$I/history?days=30" "v1/ec2/instances/$I/history?days=180"; do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s  $p\n" -H "Authorization: Bearer $TOKEN" "$API/$p"
done
curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/ec2/instances/$I/history?days=30" | jq -c '.changes[]'
```

## 4. Follow one request

Send your own request ID; the API echoes it and logs it as structured JSON. The MCP server does the same for every
tool call (Lab 07).

```bash
curl -s -D - -o /dev/null -H "x-request-id: lab-06-hello" -H "Authorization: Bearer $TOKEN" \
  "$API/v1/ec2/summary?group_by=team" | grep -i x-request-id
docker compose logs --since 1m api | grep lab-06-hello
```

## Break it, then fix it

**Predict (P3):** what status code and `detail` does each of these return?

```bash
# a) no token
curl -s -w ' %{http_code}\n' "$API/v1/ec2/summary"
# b) a valid token for the MCP server (aud=ec2-mcp), sent to the API
MT=$(api_token ec2-mcp); jwt_claims "$MT" | jq -c '{aud, scope}'
curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $MT" "$API/v1/ec2/summary"
# c) a malformed account ID
curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $TOKEN" "$API/v1/ec2/instances?account_id=123"
```

**Fix for (b):** ask the token endpoint for the right audience (`api_token`, default `ec2-api`). The MCP server does
this properly with token exchange; you'll watch it in Lab 07.

Now least privilege. As admin, create a user that can only read the latest snapshot:

```bash
AT=$(api_token ec2-api admin)
RO_USER=lab-ro-$(date +%H%M%S); RO_PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
curl -s -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' "$API/admin/users" \
  -d "{\"username\":\"$RO_USER\",\"password\":\"$RO_PW\",\"scopes\":[\"inventory:read\"]}" | jq -c .
RT=$(curl -s $API/auth/token -d username=$RO_USER --data-urlencode password=$RO_PW | jq -r .access_token)
jwt_claims "$RT" | jq -c '{sub, scope}'
```

**Predict (P4):** the read-only user asks for the instance, then its history, then tries to get a token with more
scopes than it has. What comes back each time?

```bash
curl -s -H "Authorization: Bearer $RT" "$API/v1/ec2/instances/$I" | jq -c '{state: .instance.state}'
curl -s -i -H "Authorization: Bearer $RT" "$API/v1/ec2/instances/$I/history" | grep -iE '^HTTP|www-authenticate|detail'
curl -s $API/auth/token -d username=$RO_USER --data-urlencode password=$RO_PW \
  -d 'scope=inventory:history users:admin' | jq -c '{scope}'
```

**Predict (P5):** JWTs can't be recalled once issued. If the admin disables the user now, does `$RT` keep working
for the rest of its 30 minutes?

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $AT" "$API/admin/users/$RO_USER/disable"
curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $RT" "$API/v1/ec2/summary"
```

Users you create stay in the API's SQLite file (disabled ones are harmless). There's no delete endpoint; that's
exercise material.

## Check your predictions

**P1.** `iss: ec2-inventory-api`, `sub: agent`, `aud: ec2-api`, `scope: "inventory:history inventory:read"`,
`iat`/`nbf`/`exp`, and a random `jti`. `exp - iat` is `1800`: 30 minutes. The header is
`{"alg":"RS256","kid":"…","typ":"JWT"}`, and its `kid` is the one JWKS publishes. `aud` is what stops reuse: the MCP
server only accepts `ec2-mcp`. `/auth/me` shows `"acting_party": null` because you called directly; through MCP it
shows the MCP server (Lab 07).

**P2.** Mine: hot lookup `0.019s`, 30-day history `0.064s`, 180-day history `0.197s`. The hot path scans ~1,000 rows
in memory; history reads one Iceberg file per day from S3. The 30-day history has three
changes: `pending` at 2026-09-27 06:00, `running` at 08:00, `terminated` (`Client.UserInitiatedShutdown`) at
2026-09-30 22:00.

**P3.** (a) `{"detail":"Not authenticated"} 401`. (b) `{"detail":"invalid token: Audience doesn't match"} 401`: a
perfectly valid signature, the wrong audience. (c) `422` with
`"msg":"String should match pattern '^\\d{12}$'"` and `"loc":["query","account_id"]`. 401 means "who are you?",
403 means "I know who you are, and no", 422 means "your arguments are wrong". Only the 422 is something an agent
should fix by itself and retry.

**P4.** The instance works (`{"state":"terminated"}`: it's in the latest snapshot, in state terminated). History is
`HTTP/1.1 403 Forbidden` with `{"detail":"missing scope(s): inventory:history"}` and a header
`www-authenticate: Bearer scope="inventory:read inventory:history"` telling the client what it would need. Asking
for extra scopes returns `{"scope":""}`: the API grants the intersection of what you ask for and what you have.
You didn't ask for `inventory:read`, so you didn't get it, and you didn't get the others either.

**P5.** No. Disable returns `204`, and the very next call with the old token gets
`{"detail":"user disabled"} 401`. `current_principal()` looks the user up on every request. That costs a SQLite read
per request; it buys instant revocation. With a real IdP you get the same effect from short token lifetimes plus
the IdP's session revocation.

## What to remember

- Audience binds a token to one service; scopes bind it to actions; the user lookup makes revocation immediate.
- A public JWKS lets any service verify tokens with no shared secret, and `kid` makes key rotation possible.
- Send a request ID on every call. It's what lets you follow an agent's tool call into this log.

Next: [Lab 07 · The MCP server](07-mcp-server.md).
