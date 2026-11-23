# 12 · Where the industry is heading (October 2026)

## 1. "RAG" became retrieval *as a tool* inside agents

- **Classic RAG** (2023): embed chunks → top-k vector search → stuff into prompt → answer. One shot, one index.
- **Production RAG now**: hybrid search (BM25 + vectors, fused with reciprocal rank fusion — ~half of production
  systems), a **reranker** (cross-encoder), query rewriting, metadata filters, and evals on retrieval quality.
- **Agentic RAG**: the agent decides *what* to retrieve, *from where* and *whether it has enough* — it calls a
  `search_docs` tool, a SQL/semantic-layer tool, or an API tool like `find_instances`, re-searches if needed, and cites.
  Retrieval is just one more tool.
- For **structured data like yours, don't vectorize rows.** Questions like "largest instance in account X" are
  answered exactly by APIs or SQL. Vector search is for unstructured text (runbooks, tickets, Confluence, ServiceNow
  incident notes). The pattern: **tools for structured data, RAG for documents, both available to one agent**.
- Long context (1M tokens) didn't kill RAG: it changed the trade-off. Small corpora can go straight into context
  (with prompt caching); large or changing corpora still need retrieval.

## 2. Context engineering replaced prompt engineering

The craft is now deciding *what enters the context window and when*: tool definitions, retrieved documents, tool
results, memory, history. Techniques you've already met in this repo:
- compact, bounded tool outputs ([06](06-mcp-servers.md));
- **progressive disclosure**: Agent Skills ([11](11-skills.md)), **tool search** (the model searches a large tool
  catalog and loads only the matching definitions), MCP **code mode / code execution with MCP** (the model writes a
  small program that calls tools, so intermediate data never enters the context — Anthropic reported ~98% token
  reduction on a heavy workflow);
- compaction / context editing for long sessions; prompt caching for stable prefixes.

## 3. Loops vs graphs: both, layered

Your memory ("loop agents are no longer used, graph agents are better") is half right:

| | What it is | Status in 2026 |
|---|---|---|
| ReAct via prompt parsing ("Thought/Action/Observation" text) | 2022-23 technique before native tool calling | **obsolete** |
| Autonomous unbounded loops (AutoGPT-style) | "give it a goal and let it run" | **obsolete** for production |
| **Model-in-a-loop with native tool calling** | the raw loop / Strands / Claude Code | **the core of nearly every capable agent** — Claude Code, Codex, Strands, AgentCore |
| **Graphs / workflows** | explicit nodes, edges, state, checkpoints | **the dominant way to orchestrate agents in business processes** (approvals, audit, retries, parallelism) |

The 2026 consensus: *if you can draw the flowchart in advance, it's a workflow; if the path depends on what's
discovered, use a bounded agent loop — usually as a node inside a graph*. The July 2026 "graphs vs loops" debate was
about where to put the boundary, not whether loops are dead. Better models push the boundary toward more loop,
compliance pushes toward more graph.

## 4. Protocols standardized

- **MCP** won agent ↔ tool connectivity (every major lab and cloud), and the 2026-07-28 spec made it stateless and
  enterprise-ready (gateways, caching, auth hardening). Now under the Linux Foundation's **Agentic AI Foundation**.
- **A2A** for agent ↔ agent delegation across vendors (v1.0, 150+ organizations, native in AgentCore, Azure AI
  Foundry, Google ADK). Use it when agents owned by different teams/vendors must hand off tasks; inside one service,
  plain function calls or a framework's multi-agent pattern is simpler.
- **Agent Skills** as an open standard for packaged know-how.
- **OpenTelemetry GenAI conventions** for traces/metrics across frameworks.

## 5. Managed agent runtimes

Running the loop, sandboxes, memory and identity yourself is becoming optional: **Bedrock AgentCore** (Runtime,
Gateway, Identity, Memory, Observability), **Claude Managed Agents** (Anthropic runs the loop and a per-session
container), Azure AI Foundry Agent Service, Google Agent Engine. The pattern: you own tools (MCP servers), prompts,
skills and evals; the platform owns execution, isolation and scaling.

## 6. Identity and security moved to the center

Agents act *on behalf of users*, so delegated, audience-bound, short-lived credentials (OAuth token exchange, workload
identity federation, no static API keys) are now table stakes, along with prompt-injection-aware design
(least-privilege tools, approvals for writes, treating tool output as untrusted).

## 7. Evals are the product

Teams that ship good agents have an eval set before they have a UI: tool-selection checks, answer-fact checks,
trajectory/cost budgets, run in CI on every change. Model upgrades are adopted by re-running evals, not by vibes.

## What this means for your platform, concretely

1. Keep building **MCP servers over your existing APIs** (inventory, DCIM, ServiceNow), task-shaped and read-only first.
2. Put a **semantic layer** (or curated views) in front of Iceberg for text-to-SQL, rather than letting agents write raw SQL.
3. Add **RAG over unstructured ops knowledge** (runbooks, incident postmortems) as a separate retrieval tool.
4. Start with a **single Strands or LangGraph agent** with those tools; add graph structure and approvals when you add
   write actions.
5. Build the **eval set** from real questions your team asks in Slack.

### Sources
- [Agentic RAG vs traditional RAG](https://www.progress.com/blogs/agentic-rag-vs-traditional-rag) · [RAG in production 2026: hybrid search, reranking](https://dev.to/ai_builder/rag-in-production-in-2026-beyond-naive-chunk-and-embed-3803) · [Hybrid search and reranking](https://canonical.com/blog/hybrid-search-and-reranking-a-deeper-look-at-rag)
- [Context engineering vs RAG](https://atlan.com/know/rag-vs-context-engineering/)
- [Anthropic: code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) · [MCP context bloat fixes in 2026](https://mcp.directory/blog/mcp-context-bloat-fix-2026-tool-search-code-mode-progressive-disclosure)
- [Agentic loops vs agent graphs](https://codimite.ai/blog/agentic-loops-vs-agent-graphs-why-enterprise-ai-is-moving-toward-graph-based-orchestration/) · [Graphs vs loops debate 2026](https://explainx.ai/blog/graphs-vs-loops-agentic-ai-debate-linear-andrew-ng-2026) · [Agents vs workflows 2026](https://www.promptzone.com/tara_suzuki/agents-vs-workflows-in-2026-how-to-choose-the-right-pattern-3ng6)
- [A2A at one year](https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year) · [MCP 2026-07-28 spec](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
