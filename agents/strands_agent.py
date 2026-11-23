"""Agent #3: the same agent with the Strands Agents SDK (AWS's open-source, model-driven agent SDK).

    mise exec -- python -m agents.strands_agent "tell me about instance i-0abc..., which region and what status"

Compare with the other two:
  * raw_loop.py       - you write the loop
  * langgraph_agent.py - you draw the loop as a graph (explicit control flow)
  * strands_agent.py   - you don't write a loop at all: model + tools + prompt, Strands runs the loop
                         ("model-driven": the LLM decides the control flow)

Strands-specific things shown here: MCPClient as a tool provider (it opens and closes the MCP session for
you), hooks for observability/guardrails, and a conversation manager that bounds context growth.
The same Agent runs unchanged on Bedrock (BedrockModel) or AgentCore Runtime.
"""
import time

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPClient

from agents.common import MCP_URL, MODEL, SYSTEM_PROMPT, mcp_access_token, question_from_argv, require_anthropic_key

ALLOWED_TOOLS = {"get_instance", "find_instances", "fleet_summary", "instance_history"}


class ToolAudit(HookProvider):
    """Hooks run around every tool call: a natural place for audit logs, metrics and allow-lists."""

    def register_hooks(self, registry: HookRegistry, **_) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in ALLOWED_TOOLS:   # defence in depth: refuse tools we didn't review
            event.cancel_tool = f"tool {name} is not allowed for this agent"
        event.invocation_state.setdefault("t0", {})[event.tool_use["toolUseId"]] = time.perf_counter()
        print(f"\n  -> {name}({event.tool_use['input']})")

    def after(self, event: AfterToolCallEvent) -> None:
        t0 = event.invocation_state.get("t0", {}).get(event.tool_use["toolUseId"], time.perf_counter())
        print(f"  <- {event.tool_use['name']} {event.result['status']} {time.perf_counter() - t0:.2f}s")


def build_agent() -> Agent:
    require_anthropic_key()
    model = AnthropicModel(model_id=MODEL, max_tokens=16000, params={"output_config": {"effort": "medium"}})
    ec2 = MCPClient(url=MCP_URL, headers={"Authorization": f"Bearer {mcp_access_token()}"})
    return Agent(
        name="ec2-inventory-agent",
        model=model,
        tools=[ec2],                    # a ToolProvider: Strands manages the MCP session lifecycle
        system_prompt=SYSTEM_PROMPT,
        hooks=[ToolAudit()],
        conversation_manager=SlidingWindowConversationManager(window_size=30),
    )


if __name__ == "__main__":
    agent = build_agent()
    result = agent(question_from_argv("Tell me the largest instance running in account 392918273645"))
    print(f"\n\n[stop_reason={result.stop_reason} tokens={result.metrics.accumulated_usage}]")
