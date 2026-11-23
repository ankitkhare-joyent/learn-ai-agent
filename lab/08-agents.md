# Lab 08 · The agents: the loop on top of everything

One sitting, about 40 minutes. Background: [docs/05](../docs/05-agents-and-mcp-basics.md),
[docs/07](../docs/07-agent-frameworks.md), [docs/08](../docs/08-production-agents.md). Code:
[agents/raw_loop.py](../agents/raw_loop.py), [agents/common.py](../agents/common.py).

This lab calls Claude with your API key. Each question costs a few thousand tokens (cents, not dollars). After it
you can explain:

- every hop a question takes, from your terminal to S3 and back, and where to look when one fails
- which decisions the model makes and which ones your code makes
- what bounds an agent: steps, permissions and tool errors

Answers to every **Predict** are at the bottom. The model's wording will differ on your run; the tool calls and
facts should not.

## Before you start

Read the loop once, top to bottom. It is 60 lines, and it is the whole idea:

```bash
vim +/'async def ask' agents/raw_loop.py
source lab/env.sh
```

In a second tmux pane, watch both services while you work:

```bash
docker compose logs -f --since 1s mcp api | grep --line-buffered -E 'api call|"path"' | grep --line-buffered -v healthz
```

## 1. One question, end to end

**Predict (P1):** for the question below, which tool or tools will the model call, with which arguments? How many
round trips to Claude (steps)? Which hops will appear in the second pane?

```bash
mise run -q agent:raw "what is the largest running instance in account 392918273645?"
```

**Predict (P2):** the tokens line reads `tokens in=… out=… cache_read=…`. Where did most of the input go?

Ask something that needs two tools:

```bash
mise run -q agent:raw "how did instance i-ff794db48de5443ab change over the last week?"
```

**Predict (P3):** one step or two? The raw loop runs every tool call from one model turn at once
(`asyncio.gather`). Look for it in the output.

## 2. Same question, other frameworks (optional)

LangGraph and Strands wrap the same loop. Run the first question through both and compare the tool calls:

```bash
mise run -q agent:langgraph "what is the largest running instance in account 392918273645?"
mise run -q agent:strands   "what is the largest running instance in account 392918273645?"
```

## Break it, then fix it

**Break 1: the step budget.** `AGENT_MAX_STEPS` is the hard stop for runaway loops.

**Predict (P4):** what happens with a budget of one step?

```bash
AGENT_MAX_STEPS=1 mise run -q agent:raw "what is the largest running instance in account 392918273645?"
```

**Fix:** the default is 12. A question that legitimately needs more is a sign the tools are too fine-grained.

**Break 2: point the agent at the wrong MCP server.** Port 8002 has nothing on it.

**Predict (P5):** what will this print?

```bash
MCP_URL=http://localhost:8002/mcp mise run -q agent:raw "how many instances per region?"
```

Then try it this way:

```bash
mise exec -- env MCP_URL=http://localhost:8002/mcp python -m agents.raw_loop "how many instances per region?" 2>&1 \
  | grep -E 'Error' | tail -2
```

**Fix:** nothing to fix in the code; the second command shows the failure you were trying to cause. Explain why the
first one didn't fail (look at the `[env]` section of [mise.toml](../mise.toml)).

**Break 3: a user who may not see history.** Use the read-only user from Lab 07, or create one:

```bash
AT=$(api_token ec2-api admin); RO_USER=lab-ro-$(date +%H%M%S); RO_PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' "$API/admin/users" \
  -d "{\"username\":\"$RO_USER\",\"password\":\"$RO_PW\",\"scopes\":[\"inventory:read\"]}"
```

**Predict (P6):** the agent logs in as whoever `AGENT_USERNAME` names. What will it do with a question it isn't
allowed to answer: retry, guess, or say so?

```bash
AGENT_USERNAME=$RO_USER AGENT_PASSWORD=$RO_PW mise run -q agent:raw \
  "how did instance i-ff794db48de5443ab change over the last week?"
```

When you're done, disable the user:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $AT" "$API/admin/users/$RO_USER/disable"
```

## Check your predictions

**P1.** One tool call in step 1, then the answer in step 2:

```
step 1: model requested 1 tool call(s)
  -> find_instances({"account_id": "392918273645", "state": "running", "sort_by": "vcpus", "descending": true, "limit": 5}) 0.04s, 2346 chars
[2 steps, tokens in=6 out=850 cache_read=2077]
```

The answer: several running instances tie at 16 vCPUs / 64 GiB (all `m6i.4xlarge`, named `eks-general`), out of 284
running in the account, from the 2026-09-30 22:00 UTC snapshot. The model chose `sort_by=vcpus` because the tool
description says so, and it mentioned the tie and the snapshot time because the system prompt in
[common.py](../agents/common.py) asks for both. In the second pane: `/auth/token` (the agent logging in, Lab 06),
`/auth/token/exchange` (MCP swapping the token, Lab 07), then one `api call path=/v1/ec2/instances` in the MCP log
and the matching `"path": "/v1/ec2/instances"` in the API log. The API answered from the hot cache (Lab 05).

**P2.** `in=6` uncached tokens against `cache_read` in the thousands: the system prompt and the tool definitions
(Lab 07's ~8.7 KB) are the same on every step and every run, and `cache_control` lets Claude reuse them. Your
`cache_read` depends on whether a run in the last few minutes warmed the cache. `out` is the biggest number you pay
full price for, and it includes the model's thinking.

**P3.** One step with two calls in parallel:

```
step 1: model requested 2 tool call(s)
  -> get_instance({"instance_id": "i-ff794db48de5443ab"}) 0.06s, 556 chars
  -> instance_history({"instance_id": "i-ff794db48de5443ab", "days": 7}) 0.12s, 500 chars
```

The answer: `eks-general`, `m7i.2xlarge`, eu-central-1, pending at 2026-09-27 06:00, running at 08:00, terminated
at 2026-09-30 22:00 (`Client.UserInitiatedShutdown`). `instance_history` was slower: it read Iceberg, not the cache.

**Section 2 (optional).** On my run LangGraph made the same single `find_instances` call. Strands made it too, then
a second one with `sort_by=memory_gib` to check the tie; its token line reports `inputTokens: 11335` in total and
doesn't break out cache reads the way `raw_loop` does. Same model, prompt and tools: the extra call is run-to-run variation, which is why
[docs/13](../docs/13-exercises.md) exercise 8 builds an eval set instead of trusting one run.

**P4.** `step 1: model requested 1 tool call(s)`, the tool runs, then
`Stopped after 1 steps without a final answer (raise AGENT_MAX_STEPS if this is legitimate).` The tool result was
never sent back to the model. The loop's exit is your code's decision, not the model's.

**P5.** It works and prints a per-region table (us-east-1 508, eu-central-1 244, ap-northeast-1 143,
ap-southeast-1 102). mise's `[env]` sets `MCP_URL = "http://localhost:8001/mcp"`, and for `mise run` and
`mise exec` that wins over the variable you set on the command line. With `env` *inside* `mise exec`, your value
is applied last, and the agent fails with `httpx.ConnectError: All connection attempts failed`. Worth knowing
before you spend an hour debugging a variable that was never used. `AGENT_MAX_STEPS` and `AGENT_USERNAME` work from
the command line because mise doesn't set them.

**P6.** It says so:

```
step 1: model requested 2 tool call(s)
  -> instance_history({"instance_id": "i-ff794db48de5443ab", "days": 7}) ERROR 0.03s, 157 chars
  -> get_instance({"instance_id": "i-ff794db48de5443ab"}) 0.05s, 556 chars
```

and the answer starts like "I can't show the last week of history for this instance because your account lacks the
`inventory:history` permission", then gives what it *can* see (the current record). No retry, no workaround. Three
layers made that happen: the API refused (scope), the MCP server phrased the refusal for a model ("tell them instead
of retrying"), and the system prompt says "If the user lacks permission for something, say so plainly."

## What to remember

- The agent is a loop. The model picks tools and arguments; your code owns the budget, the credentials and when to
  stop.
- Every hop has its own log and its own ID. Lab 06's request ID is what ties an answer back to a query.
- Permissions belong in the API, not the prompt. The prompt only decides how the refusal is explained.

You've now been through every component. [docs/13-exercises.md](../docs/13-exercises.md) has the next level: changing
tools, adding approval steps and building an eval set.
