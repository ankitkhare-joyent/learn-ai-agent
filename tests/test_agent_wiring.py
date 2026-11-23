"""Agent plumbing tests that need no LLM: a scripted fake model drives the real graph and real MCP tools."""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agents import langgraph_agent


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


async def test_langgraph_agent_calls_mcp_tool_then_answers():
    tools = await langgraph_agent.load_tools()
    model = ScriptedModel(messages=iter([
        AIMessage("", tool_calls=[{"name": "find_instances", "id": "call_1", "type": "tool_call",
                                   "args": {"account_id": "392918273645", "state": "running",
                                            "sort_by": "vcpus", "descending": True, "limit": 1}}]),
        AIMessage("The largest running instance is an m6i.4xlarge."),
    ]))
    app = langgraph_agent.build_graph(model, tools)
    out = await app.ainvoke({"messages": [HumanMessage("largest?")]}, {"configurable": {"thread_id": "t1"}})
    tool_msg = next(m for m in out["messages"] if m.type == "tool")
    assert "392918273645" in str(tool_msg.content) and tool_msg.status != "error"
    assert out["messages"][-1].content.startswith("The largest")


def test_strands_sees_mcp_tools():
    from strands.tools.mcp import MCPClient

    from agents.common import MCP_URL, mcp_access_token
    with MCPClient(url=MCP_URL, headers={"Authorization": f"Bearer {mcp_access_token()}"}) as client:
        names = {t.tool_name for t in client.list_tools_sync()}
    assert {"get_instance", "find_instances"} <= names
