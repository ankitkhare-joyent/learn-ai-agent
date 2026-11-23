# 07 · Agent frameworks: raw loop, LangGraph, Strands

Same agent, three ways, same MCP server, same system prompt ([agents/common.py](../agents/common.py)):

| File | You write | The framework gives you |
|---|---|---|
| [raw_loop.py](../agents/raw_loop.py) | the loop, tool conversion, parallel execution, step limit | nothing (the Anthropic SDK + MCP client only) |
| [langgraph_agent.py](../agents/langgraph_agent.py) | a **graph**: nodes (agent, tools) and edges | checkpointed state, multi-turn threads, recursion limit, streaming, interrupts for human approval |
| [strands_agent.py](../agents/strands_agent.py) | model + tools + prompt | the loop itself, MCP lifecycle, hooks, conversation management, OpenTelemetry tracing, multi-agent patterns, AWS deployment |

```bash
mise run agent:raw       "tell me about instance i-..., which region is it in and what is the status"
mise run agent:langgraph "tell me the largest instance running in account 392918273645"
mise run agent:langgraph -- --chat          # multi-turn: ask a follow-up like "and the second largest?"
mise run agent:strands   "how many GPUs are running right now, per region?"
```

## The raw loop (read it first)

Everything else is built on this. Things it does that every production agent needs:
- **Step limit** (`AGENT_MAX_STEPS`): a model that keeps calling tools must be stopped.
- **Parallel tool calls**: the model can ask for several tools in one turn; run them concurrently and return all
  results in one message.
- **Errors as data**: a failed tool returns `is_error: true` with a message; the model reads it and recovers.
- **Keep the full assistant content** (thinking + tool_use blocks), not just text, when appending history.
- **Prompt caching** (`cache_control`): tools + system prompt + history are re-sent every step; caching makes later
  steps ~10× cheaper on the cached part.
- **Token accounting** printed at the end: agents multiply cost by the number of steps.

Anthropic's SDK also has a **Tool Runner** (`client.beta.messages.tool_runner`) that runs this loop for you with hooks;
it's the "no framework, less code" option.

## LangGraph

**What it is**: a library (by LangChain) for building agents and workflows as **state machines**. You define a typed
`State`, **nodes** (functions that read state and return updates), and **edges** (fixed or conditional). LangGraph
runs it, **checkpointing state after every node**.

```
START → agent ──tool calls?──► tools ─┐
          ▲                            │
          └────────────────────────────┘
          └──no tool calls──► END
```

Why people choose it:
- **Explicit control flow**: you can mix deterministic steps with LLM steps — a router node, a guardrail node, a
  "summarize result" node, parallel branches, subgraphs — and see exactly what can happen.
- **Durable execution**: checkpointers (InMemory → SQLite → **Postgres** in production) let a run resume after a crash,
  give you multi-turn memory per `thread_id`, and "time travel" for debugging.
- **Human-in-the-loop**: `interrupt()` pauses the graph (state saved), a human approves/edits, the graph resumes —
  essential for write actions ("terminate this instance?").
- **Streaming** of node updates/tokens to a UI.
- Ecosystem: LangSmith (tracing/evals), LangGraph Platform/Server for deployment, `langchain-mcp-adapters` for MCP.

Costs: more concepts and boilerplate; LangChain message types; version churn historically (1.x is now stable).

## Strands Agents SDK

**What it is**: AWS's open-source agent SDK (Python and TypeScript, 1.x since May 2026), the one AWS uses internally
and recommends for **Bedrock AgentCore**. It's **model-driven**: you give it a model, tools and a prompt, and the model
drives the loop — no graph to draw.

What it gives you:
- **Any model provider** (Bedrock, Anthropic, OpenAI, Ollama, LiteLLM…); the same agent moves from laptop to Bedrock by
  swapping `AnthropicModel` for `BedrockModel`.
- **MCP as a first-class tool provider** (`tools=[MCPClient(url=…)]`) and plain Python `@tool` functions.
- **Hooks** around model and tool calls (used here for an allow-list and timing), **conversation managers** to bound
  context, session managers for persistence, **OpenTelemetry tracing built in**.
- **Multi-agent patterns**: agents-as-tools, **Swarm** (agents hand off to each other), **Graph** (a flowchart of
  agents, with LLM-chosen branches), **Workflow** (fixed DAG), and **A2A** for cross-service agents.
- Deployment paths: AgentCore Runtime (serverless, per-session isolation), Lambda, Fargate, EKS.

## Which should you use?

| Situation | Pick |
|---|---|
| Learning, or a tiny focused agent | raw loop / Anthropic Tool Runner |
| Your org is on AWS, you want Bedrock/AgentCore, quick model-driven agents, A2A | **Strands** |
| You need explicit, auditable control flow, durable long-running runs, human approval steps, complex multi-agent graphs | **LangGraph** |
| Coding/filesystem-heavy agents | Claude Agent SDK (Claude Code as a library) |
| Anthropic runs the loop and the sandbox for you | Claude Managed Agents |

For your platform: the read-only inventory assistant is a perfect **Strands** fit (simple, AWS-native). Anything that
*changes* infrastructure (stop idle instances, open a ServiceNow change) is a **LangGraph** fit: a graph with a
mandatory approval interrupt before the write tool.

## "Loop agents are dead, graphs won" — not quite

What actually happened (see [12-industry-direction.md](12-industry-direction.md)):
- The **inner loop** (model calls tools until done) is still how essentially every capable agent works, including
  Claude Code, Codex and Strands. As models got better at planning, the loop got *more* useful, not less.
- What moved to **graphs** is the **orchestration around** the loop in enterprise systems: explicit steps, approvals,
  retries, parallel branches, audit. The common production shape is **a graph whose nodes include bounded loop
  agents**.
- The old thing that *did* die is the 2023 style of hand-written ReAct prompt parsing ("Thought: … Action: …") and
  autonomous "AutoGPT" loops with no structure. Native tool calling replaced the parsing; structure replaced the
  unbounded autonomy.

### Sources
- [LangGraph docs](https://langchain-ai.github.io/langgraph/) · [LangGraph state, checkpoints, recovery](https://eastondev.com/blog/en/posts/ai/20260424-langgraph-agent-architecture/)
- [Strands Agents docs](https://strandsagents.com/) · [Strands multi-agent patterns](https://strandsagents.com/docs/user-guide/sdk/multi-agent/multi-agent-patterns/) · [Strands 1.0 announcement](https://aws.amazon.com/blogs/opensource/introducing-strands-agents-1-0-production-ready-multi-agent-orchestration-made-simple/)
- [Loops vs graphs (2026)](https://codimite.ai/blog/agentic-loops-vs-agent-graphs-why-enterprise-ai-is-moving-toward-graph-based-orchestration/) · [Stanford CS224G 2026 lecture 7: agent orchestration](https://web.stanford.edu/class/cs224g/)
