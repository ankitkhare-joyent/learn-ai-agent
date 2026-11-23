# 08 · Production guidelines for agents

A checklist you can hold a real agent to. Each item says what to do and how this repo does it (or where it stops).

## 1. Scope and design
- **Start with a workflow; add autonomy only where the path is unknown.** ([05](05-agents-and-mcp-basics.md#4-workflow-or-agent))
- **One agent, one job**, with a tool set sized to that job (here: 4 read-only tools). Split into multiple agents only
  when tool sets or permissions genuinely differ.
- **Read vs write separation**: read tools can be auto-approved; write tools live on a separate MCP server and require
  human approval (LangGraph `interrupt()`, or the host's confirmation UI driven by `destructiveHint`).

## 2. Evaluation (do this before you tune anything)
- Build a **golden set**: 30-100 real questions with expected facts and expected tool calls, e.g.
  `"largest running instance in 392918273645" → find_instances(account_id=…, sort_by="vcpus", descending=true); answer mentions m6i.4xlarge`.
- Grade at three levels: **tool selection/arguments** (deterministic checks), **final answer facts** (deterministic where
  possible, otherwise an LLM judge with a rubric), **trajectory** (steps, cost, latency budgets).
- Run it in CI on every prompt, tool-description or model change, and compare to the previous run.
- Because data changes every 2 hours, evaluate against a **frozen snapshot** (`snapshot_ts`) or assert relationships
  ("the answer's instance is in the top-5 by vCPU") rather than exact IDs.
- [tests/test_agent_wiring.py](../tests/test_agent_wiring.py) shows the cheapest layer: a scripted fake model drives
  the real graph and real MCP tools, so plumbing is tested without LLM cost.

## 3. Observability
- **Trace every run**: one trace per user question; spans for each model call (model, tokens in/out/cached, latency,
  stop reason) and each tool call (name, args, duration, error), carrying the request ID into the API logs
  (the MCP server sends `x-request-id`).
- Standard: **OpenTelemetry GenAI semantic conventions** (agent, model, MCP spans and metrics; still "Development"
  status, moving fast). Strands emits OTel natively; LangGraph via LangSmith or OpenInference; then send to
  Grafana/Tempo, Datadog, Langfuse, Phoenix, or LangSmith.
- **Dashboards**: success rate, steps per answer, tokens and $ per answer, tool error rate, p95 latency.

## 4. Guardrails and safety
- **Hard limits**: max steps (here `AGENT_MAX_STEPS`/`recursion_limit`), max tokens, wall-clock timeout, tool timeouts.
- **Least privilege**: the agent acts with the *user's* permissions (token exchange end to end, [06](06-mcp-servers.md#3-security-model)),
  never with a superuser service account.
- **Prompt injection**: anything a tool returns is untrusted data (tags, ticket text, web pages). Mitigations: keep
  high-impact tools behind approval; don't let tool output pick URLs, recipients or credentials; tool allow-lists
  (the Strands hook here); separate "read untrusted content" agents from "take actions" agents.
- **Input/output validation**: schemas on tool inputs (MCP), structured outputs for anything code consumes.
- **PII/secrets**: redact in logs and traces; never put secrets in prompts.

## 5. Cost and latency
- **Prompt caching**: stable prefix first (tools, system prompt), volatile content last. Check
  `cache_read_input_tokens` > 0 on the 2nd+ step.
- **Small outputs from tools** (the biggest lever after caching): every tool result is re-sent on every later step.
- **Effort/model choice per route**: `effort: "medium"` here; measure before raising. Use a cheaper model only after
  your eval shows it holds quality.
- **Tool search / progressive disclosure** when you have many tools (see [12](12-industry-direction.md)).
- Budget per conversation and alert on outliers (runaway loops show up as cost spikes first).

## 6. State and memory
- **Short-term**: the conversation (bounded by a conversation manager or compaction).
- **Durable run state**: LangGraph checkpointer on Postgres / Strands session manager on S3/DynamoDB, so any replica
  can continue a conversation.
- **Long-term memory** (user preferences, facts learned): a separate store the agent reads/writes via tools — and treat
  it as untrusted input too.

## 7. Deployment
- **On your EKS**: agents are ordinary stateless services (FastAPI/WebSocket front, state in Postgres/Redis), scaled by
  HPA/KEDA. The MCP server is a separate Deployment. Put an MCP/API gateway in front for auth and rate limits.
- **Bedrock AgentCore**: Runtime (serverless, session isolation, long-running), Gateway (MCP tools from
  Lambdas/OpenAPI), Identity (inbound auth + outbound credential vault), Memory, Observability. Strands runs there
  unchanged.
- **Ray Serve** if you already run Ray and need GPU models next to the agent; otherwise unnecessary for an agent that
  calls a hosted LLM.

## No API keys: workload identity federation

On your laptop an API key in `.env` (or in OpenBao: `mise run set-anthropic-key`) is fine. In production, **don't
store a Claude API key at all**. The Claude API supports **workload identity federation (WIF)**: your platform's
identity provider issues a short-lived signed token (a JWT) for the workload; Anthropic verifies it against a
*federation rule* you configured, and returns a short-lived access token. Nothing static to leak or rotate.

```
pod / job ──► platform identity (OIDC JWT, minutes) ──► Anthropic /v1/oauth/token (federation rule) ──► short-lived token ──► Claude API
```

The SDKs do this automatically when these env vars are set (no code changes; [agents/common.py](../agents/common.py)
already accepts them):

```bash
ANTHROPIC_FEDERATION_RULE_ID=...        # the rule that maps "this workload identity" to an Anthropic service account
ANTHROPIC_ORGANIZATION_ID=...
ANTHROPIC_SERVICE_ACCOUNT_ID=...
ANTHROPIC_IDENTITY_TOKEN_FILE=/var/run/secrets/...   # where the platform writes the workload's OIDC token
```

How it maps to your environments:

| Where | Identity token comes from | Federation rule trusts |
|---|---|---|
| **GitHub Actions** (evals in CI) | `permissions: id-token: write`; the runner's OIDC token for `repo:org/repo:ref:refs/heads/main` | issuer `token.actions.githubusercontent.com`, subject = your repo/branch |
| **EKS** (the agent / RayService pods) | a **projected service-account token** with audience set for Anthropic (`serviceAccountToken` volume, `audience: <anthropic>`), mounted at a file path | your cluster's OIDC issuer (the same one IRSA uses), subject = `system:serviceaccount:<ns>:<sa>` |
| **AWS (Lambda, ECS, EC2)** | an OIDC token issued for the workload's role (check the WIF docs for the AWS issuers currently supported) | that issuer + the role |
| **GCP / Azure** | workload identity tokens | their issuers |

For your RayService agent on EKS that means: give the Ray worker pods a dedicated ServiceAccount, mount a projected
token with Anthropic's audience, set the four env vars, and create one federation rule per environment
(dev/stage/prod) bound to that namespace/ServiceAccount. Revoking access = deleting the rule; there's no key to hunt down.
Keep API keys only for laptops and tools without an identity provider — the same rule the Console states.

The same principle applies to everything else the agent touches: AWS via **IRSA / EKS Pod Identity**, CI via
**GitHub OIDC**, and your own APIs via the user's delegated token. OpenBao/Secrets Manager then holds only the
secrets that have no federation story (third-party API keys, DB passwords).

### Sources
- [Claude API authentication options (incl. WIF)](https://platform.claude.com/docs/en/manage-claude/authentication) · [WIF reference](https://platform.claude.com/docs/en/manage-claude/wif-reference) · [WIF admin (issuers, rules)](https://platform.claude.com/docs/en/manage-claude/wif-admin-api)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) · [status in 2026](https://www.truefoundry.com/blog/opentelemetry-genai-semantic-conventions)
- [LangGraph production practices](https://dev.to/young_gao/building-production-ai-agents-with-langgraph-beyond-the-toy-examples-2idm)
- [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/) · [Strands deep dive](https://aws.amazon.com/blogs/machine-learning/strands-agents-sdk-a-technical-deep-dive-into-agent-architectures-and-observability/)
- [GitHub Actions OIDC](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect) · [EKS service account token projection](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#serviceaccount-token-volume-projection)
