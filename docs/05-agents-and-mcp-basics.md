# 05 · Agents and MCP from first principles

Read this before the code in [agents/](../agents). It answers: what is an LLM tool call, what is an agent, what is MCP,
and how do they differ.

## 1. Three building blocks

### An LLM call
Text in, text out. The model has no access to your systems and no memory between calls: you send the whole
conversation every time.

### A tool call (function calling)
You tell the model which functions exist (name, description, JSON schema of the arguments). Instead of answering, the
model can reply *"call `get_instance` with `{"instance_id": "i-0abc"}`"*. **The model never runs anything.** Your code
runs the function and sends the result back as the next message.

```
you   -> model: question + tool definitions
model -> you:   tool_use get_instance {"instance_id": "i-0abc"}        (stop_reason = "tool_use")
you:            run it -> {"state": "running", "region": "us-east-1"}
you   -> model: tool_result {...}
model -> you:   "i-0abc is running in us-east-1 (as of 22:00 UTC)."   (stop_reason = "end_turn")
```

### An agent
**An LLM calling tools in a loop until it decides it's done.** That's the whole definition:

```python
while True:
    response = llm(system_prompt, tools, messages)
    if no tool calls in response: return response.text
    results = run(tool_calls)                 # possibly in parallel
    messages += [response, results]
```

[agents/raw_loop.py](../agents/raw_loop.py) is exactly this in ~60 lines, against the real MCP server. The model chooses
*which* tools to call and *when to stop*; that autonomy is what makes it an agent instead of a script.

What makes an agent good is mostly not the loop:
- **tools** that are well-named, well-described, narrow, and return compact results;
- **context**: the system prompt, what history you keep, what tool output you feed back (this is "context engineering");
- **guardrails**: step limits, permissions, approvals for risky actions;
- **evaluation**: a test set of questions with known answers that you run on every change.

## 2. Where MCP fits

Before MCP, every app wired every tool by hand: a LangChain tool for Slack, a different Strands tool for Slack, a third
for Claude Desktop. **MCP (Model Context Protocol) is a standard plug between AI applications and the systems they use**,
so a tool is written once and works in any MCP-capable host (Claude Desktop/Code, ChatGPT, Cursor, LangGraph, Strands,
Bedrock AgentCore, …).

```
                 MCP host (the AI app)                         MCP server (your system's adapter)
┌──────────────────────────────────────────┐            ┌────────────────────────────────────┐
│  LLM  <->  agent loop  <->  MCP client ───┼── JSON-RPC ─┼──> tools / resources / prompts     │
└──────────────────────────────────────────┘ stdio or   │      │                              │
                                             HTTP        │      └──> your REST API, DB, SaaS…  │
                                                         └────────────────────────────────────┘
```

An MCP server exposes three kinds of things:

| Primitive | Who decides to use it | In this repo |
|---|---|---|
| **Tools** — functions with JSON-schema inputs (and optionally output schemas) | the **model** | `get_instance`, `find_instances`, `fleet_summary`, `instance_history` |
| **Resources** — readable data addressed by URI | the **application / user** attaches them | `inventory://schema` (field glossary) |
| **Prompts** — reusable prompt templates with arguments | the **user** picks them (e.g. a slash command) | `investigate_instance` |

Transports: **stdio** (the host launches the server as a subprocess; local tools, credentials from env) and
**Streamable HTTP** (a remote service; OAuth 2.1 protected). The 2026-07-28 spec made the protocol **stateless**: no
handshake/session is required, each request carries its protocol version and capabilities, so a server scales like any
HTTP API.

## 3. MCP vs agents vs APIs: the differences people mix up

| | REST API | MCP server | Agent |
|---|---|---|---|
| What it is | an interface for programs | an interface for **AI applications** (a standard tool/data adapter) | a **program that uses an LLM to decide** what to do |
| Has an LLM inside? | no | **no** (usually) | yes |
| Decides what to call? | no — the caller does | no — the model in the host does | yes |
| Audience of the docs | developers | **models** (descriptions are prompts) | users |
| Example here | `GET /v1/ec2/instances?account_id=…` | `find_instances(account_id=…, sort_by="vcpus")` | "what's the largest instance in account 3929…?" |

Key points:
- **MCP is not an agent framework.** It's plumbing. LangGraph and Strands are agent frameworks; both *consume* MCP tools.
- **An MCP server is a thin, opinionated layer over your API**: fewer, task-shaped tools, LLM-friendly descriptions,
  compact outputs, actionable errors, and auth designed for delegated access. Auto-generating one tool per endpoint
  works for prototypes but usually gives worse agents.
- **Function calling vs MCP**: function calling is the model-API feature (the `tools` parameter). MCP is how tools get
  *discovered and executed* across process/network boundaries. `raw_loop.py` shows the bridge: `list_tools()` → the
  `tools` parameter; `tool_use` block → `call_tool()`.
- **A2A (Agent-to-Agent)** is the other protocol you'll hear about: agent ↔ agent delegation (task lifecycle, agent
  cards), where MCP is agent ↔ tool. Both are now under the Linux Foundation's Agentic AI Foundation.

## 4. Workflow or agent?

Not everything should be an agent. Anthropic's rule of thumb, still the industry default: **use the simplest thing that
works.**

| Pattern | Control flow decided by | Use when | Example here |
|---|---|---|---|
| Single LLM call | — | classify, extract, summarize | "summarize this instance JSON" |
| **Workflow** (prompt chain, router, parallel fan-out) | **your code** | the steps are known in advance | nightly "fleet change report": fetch summary → diff vs yesterday → LLM writes prose |
| **Agent** | **the model** | the steps depend on what it discovers | "why did spend in account X jump?" |

If you can draw the full flowchart before running, write a workflow (a LangGraph graph with fixed edges is a workflow).
If the path depends on intermediate results, use an agent — ideally a bounded one inside a workflow.

## 5. The two example questions, traced

**"Tell me about instance i-0abc…, which region is it in and what is the status?"**
1. The model sees the tool list, picks `get_instance(instance_id="i-0abc…")`.
2. MCP server validates the token, exchanges it for an API token, calls `GET /v1/ec2/instances/i-0abc…`.
3. API answers from the in-memory latest snapshot (≈ms). If the instance is gone, it searches Iceberg history.
4. The model answers with region, AZ, state, and the snapshot time.

**"Tell me the largest instance running in account 392918273645"**
1. The tool description says *"for largest use sort_by=vcpus, descending=true"*, so the model calls
   `find_instances(account_id="392918273645", state="running", sort_by="vcpus", descending=true, limit=5)`.
2. It gets 5 rows + `total_matches`, sees ties (several 16-vCPU boxes), may call again sorted by `memory_gib`, and answers.

Run both against the three agents and compare the traces (`mise run agent:raw "…"`, `agent:langgraph`, `agent:strands`).

### Sources
- [Model Context Protocol specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) and the [release post](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [A2A protocol at one year (Linux Foundation)](https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
