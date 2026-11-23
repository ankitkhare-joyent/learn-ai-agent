# Lab 01 · OpenBao: where every credential comes from

One sitting, about 30 minutes. Background: [docs/09](../docs/09-secrets-openbao.md). Code:
[scripts/init_openbao.py](../scripts/init_openbao.py), [scripts/bao_exec.py](../scripts/bao_exec.py),
[agents/secrets.py](../agents/secrets.py).

OpenBao comes first because nothing else can start without it: S3, the API and the MCP server all get their
credentials from it at boot. After this lab you can explain:

- what your developer token is allowed to do, and how you'd find out without reading code
- how a secret gets from OpenBao into a running process without ever appearing in `compose.yaml` or `docker inspect`
- why a missing secret can surface three hops later as a confusing error

Answers to every **Predict** are at the bottom. Write your guess down (a scratch buffer in vim is fine) before you run
the command.

## Before you start

```bash
cd ~/workstuff/learn-ai-agent
source lab/env.sh
docker ps --format '{{.Names}}\t{{.Status}}' | grep ec2-lakehouse     # 6 containers, all Up
```

## 1. Is the vault open?

**Predict (P1):** the stack has been running for a while. Is OpenBao sealed? How many key shares does it take to
unseal it here, and how many would you want in production?

```bash
curl -s $BAO_ADDR/v1/sys/health | jq '{initialized, sealed, version}'
curl -s $BAO_ADDR/v1/sys/seal-status | jq '{type, t, n, storage_type}'
```

These two endpoints need no token: health checks and load balancers call them.

## 2. Who am I?

**Predict (P2):** your dev token came from `init_openbao.py`. Which policies does it carry, and roughly how long
until it expires?

```bash
curl -s -H "X-Vault-Token: $BAO_TOKEN" $BAO_ADDR/v1/auth/token/lookup-self \
  | jq '.data | {display_name, policies, ttl_days: (.ttl / 86400 | floor), renewable}'
```

## 3. What can I see, and what can I read?

**Predict (P3):** you'll list the secret names under `ec2-lakehouse/`, then read each one's field names (not the
values: `bao_keys` in [env.sh](env.sh) strips them). Which of the five will say `permission denied`?

```bash
curl -s -X LIST -H "X-Vault-Token: $BAO_TOKEN" $BAO_ADDR/v1/secret/metadata/ec2-lakehouse | jq -c .data.keys
for s in agent admin s3 mcp-client api; do printf '%-11s' $s; bao_keys ec2-lakehouse/$s; done
```

Now find the rule that produced that answer. Open the policy in vim and look for the `developer` block:

```bash
vim +/'"developer"' scripts/init_openbao.py
```

**Predict (P4):** can your token read its own policy over the API?

```bash
curl -s -H "X-Vault-Token: $BAO_TOKEN" $BAO_ADDR/v1/sys/policies/acl/developer | jq -c .
```

## 4. How a container gets its secrets

The API container's config only names *where* its secrets live:

```bash
docker compose config --format json api | jq -r .services.api.environment.BAO_SECRETS | tr ';' '\n'
```

**Predict (P5):** if you open a shell in the API container and print its environment, will you see
`S3_SECRET_KEY` and `API_JWT_PRIVATE_KEY`?

```bash
docker compose exec api sh -c 'env | cut -d= -f1 | grep -E "^(S3_|API_)" | sort'
```

Now look at the environment of the process that is actually serving requests, PID 1 (names only, again):

```bash
docker compose exec api sh -c "tr '\0' '\n' < /proc/1/environ | cut -d= -f1 | grep -E '^(S3_|API_)' | sort"
docker compose exec api sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
docker compose logs api | grep bao_exec
```

**Predict (P6):** PID 1 is `uvicorn`, not `bao_exec.py`. Why?

Each container mounts only its own AppRole folder. Compare:

```bash
docker compose exec api ls /creds
docker compose exec mcp ls /creds
```

## Break it, then fix it

**Break 1: a service asks for a secret its policy doesn't allow.** Run `bao_exec.py` inside the API container,
asking for your Anthropic key path (the `api` policy doesn't list it):

```bash
docker compose exec api sh -c \
  'BAO_SECRETS="X=ec2-lakehouse/anthropic#api_key" python scripts/bao_exec.py true' 2>&1 | tail -2
```

**Predict (P7):** login succeeded, the read failed. Look at the end of `main()` in
[bao_exec.py](../scripts/bao_exec.py). What happened to the AppRole token it just logged in with? What would you change?

**Break 2: the agent can't reach OpenBao.** Point your laptop at a bad token and ask for an MCP token the way every
agent does:

```bash
BAO_TOKEN=not-a-token mise exec -- python -c \
  "from agents.common import mcp_access_token; mcp_access_token()" 2>&1 | grep '^httpx'
```

**Predict (P8):** the root cause is OpenBao rejecting the token. Which service, and which status code, will the error
actually name? Read `read_secret()` in [agents/secrets.py](../agents/secrets.py) and `mcp_access_token()` in
[agents/common.py](../agents/common.py) to find out why.

**Fix:** nothing to repair; the bad token only lived in that one command. Run it again without the override and
check you get no error:

```bash
mise exec -- python -c "from agents.common import mcp_access_token; print(len(mcp_access_token()), 'char token')"
```

## Check your predictions

**P1.** `{"initialized": true, "sealed": false, "version": "2.6.2"}` and
`{"type": "shamir", "t": 1, "n": 1, "storage_type": "raft"}`: one key share, threshold one. That's a local-only
shortcut. In production you'd use KMS auto-unseal (no human holds a key), with recovery keys split across several
people (for example 5 shares, threshold 3).

**P2.** `policies: ["default", "developer"]`, `ttl_days` around 22 out of the 30 days it was created with (`720h`),
`renewable: true`. The number depends on when you last ran `mise run up` with an expired token.

**P3.** The listing shows all five names: `["admin","agent","api","mcp-client","s3"]`. Your Anthropic key is in
`.env`, so `anthropic` isn't there. The developer policy grants `list` on metadata, so you can see that `api` and
`mcp-client` exist, but reading them is denied:

```
agent      {"fields":["password","username"],"version":1}
admin      {"fields":["password","username"],"version":1}
s3         {"fields":["access_key","secret_key"],"version":1}
mcp-client {"errors":["1 error occurred:\n\t* permission denied\n\n"]}
api        {"errors":["1 error occurred:\n\t* permission denied\n\n"]}
```

`version: 1` means nothing has rotated these yet (`put_once` writes with check-and-set 0, so restarts never do).

**P4.** No: `permission denied`. Policies are default-deny, and `developer` grants nothing under `sys/`. Reading the
source is how a human answers "what can this token do"; in production you'd use `bao token capabilities <path>`.

**P5.** No. The `exec` shell only shows `API_USERS_DB` and the non-secret `S3_ENDPOINT*`/`S3_REGION`. `docker compose
exec` starts a new process from the container's config, and the secrets were never in that config.

**P6.** PID 1's environment has all of them: `API_ADMIN_PASSWORD`, `API_AGENT_PASSWORD`, `API_JWT_PRIVATE_KEY`,
`API_MCP_CLIENT_SECRET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and so on. `bao_exec.py` logged in, put the secrets into
its own environment, then called `os.execvp`, which *replaces* the process image with uvicorn while keeping the PID
and the environment. The log line `bao_exec: loaded 5 secret path(s), starting uvicorn` is the last thing it ever
printed (you'll see it once per container start; `docker compose logs` keeps earlier starts too). Both containers list only `role_id secret_id`, but each is a different AppRole (compose mounts
`bao-creds` with a different `subpath` per service).

**P7.** The traceback ends in `urllib.error.HTTPError: HTTP Error 403: Forbidden`. The secret loop raised before
`revoke-self`, so that AppRole token stays valid until its 1-hour TTL. A tidier version wraps the reads in
`try/finally` and revokes in the `finally`. Small, but it's the kind of leak an audit finds.

**P8.** `httpx.HTTPStatusError: Client error '422 Unprocessable Content' for url 'http://localhost:8000/auth/token'`.
The error names the **API**, with a **422**, not OpenBao and not a 403. `read_secret()` returns `{}` for any non-200,
so `mcp_access_token()` falls back to username `agent` and an empty password, and FastAPI rejects the empty required
form field. Lesson: a silent fallback moves the error away from its cause. When you debug an auth failure, check
the step *before* the one that failed.

## What to remember

- Secrets live in exactly one place; containers get them at start and never write them to config or images.
- `docker compose exec` doesn't show you what the real process sees; `/proc/1/environ` does.
- Least privilege is visible from the outside: you can *list* a secret you can't *read*.

Next: [Lab 02 · S3](02-s3-storage.md).
