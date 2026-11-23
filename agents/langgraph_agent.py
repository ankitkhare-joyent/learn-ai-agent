"""Agent #2: the same agent as an explicit LangGraph state machine.

    mise exec -- python -m agents.langgraph_agent "tell me the largest instance running in account 392918273645"
    mise exec -- python -m agents.langgraph_agent --chat          # multi-turn; the checkpointer remembers

The graph:

        START -> agent --(tool calls?)--> tools
                   ^                        |
                   +------------------------+
                   |
                   +--(no tool calls)--> END

That is the raw loop from agents/raw_loop.py, drawn as nodes and edges. What LangGraph adds:
  * state that is checkpointed after every node (resume after a crash, multi-turn memory, time travel)
  * a recursion limit (no runaway loops), streaming of each step, interrupts for human approval
  * a place to add more nodes later (a router, a guardrail, a summarizer) without rewriting the loop
"""
import argparse
import asyncio
import uuid

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agents.common import MAX_STEPS, MCP_URL, MODEL, SYSTEM_PROMPT, mcp_access_token, require_anthropic_key


def build_graph(model: BaseChatModel, tools: list, checkpointer=None):
    """Pure graph construction - no I/O - so tests can pass a fake model and fake tools."""
    model_with_tools = model.bind_tools(tools)

    async def agent(state: MessagesState):
        response = await model_with_tools.ainvoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    # handle_tool_errors: a failing tool becomes a ToolMessage the model can read and recover from,
    # instead of an exception that kills the run.
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    # Production: PostgresSaver (langgraph-checkpoint-postgres) so any replica can resume any thread.
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


async def load_tools() -> list:
    client = MultiServerMCPClient({
        "ec2": {"transport": "streamable_http", "url": MCP_URL,
                "headers": {"Authorization": f"Bearer {mcp_access_token()}"}},
    })
    return await client.get_tools()


def make_model() -> ChatAnthropic:
    require_anthropic_key()
    return ChatAnthropic(model=MODEL, max_tokens=16000, output_config={"effort": "medium"}, max_retries=3)


async def run_turn(app, question: str, thread_id: str) -> str:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 2 * MAX_STEPS + 1}
    final = None
    # stream_mode="updates" yields one event per node execution: a free execution trace.
    async for update in app.astream({"messages": [HumanMessage(question)]}, config, stream_mode="updates"):
        for node, payload in update.items():
            msg = payload["messages"][-1]
            if node == "agent" and isinstance(msg, AIMessage) and msg.tool_calls:
                for call in msg.tool_calls:
                    print(f"  agent -> {call['name']}({call['args']})")
            elif node == "tools":
                for m in payload["messages"]:
                    print(f"  tools <- {m.name}: {len(str(m.content))} chars{' (error)' if m.status == 'error' else ''}")
            else:
                final = msg
    return final.text if final else ""


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="*")
    p.add_argument("--chat", action="store_true", help="interactive multi-turn session")
    args = p.parse_args()
    app = build_graph(make_model(), await load_tools())
    thread_id = str(uuid.uuid4())   # one conversation = one thread; the checkpointer keys state by it
    if not args.chat:
        q = " ".join(args.question) or "Tell me the largest instance running in account 392918273645"
        print(await run_turn(app, q, thread_id))
        return
    print("EC2 inventory agent (LangGraph). Ctrl-D to quit.")
    while True:
        try:
            q = input("\nyou> ").strip()
        except EOFError:
            break
        if q:
            print("\n" + await run_turn(app, q, thread_id))


if __name__ == "__main__":
    asyncio.run(main())
