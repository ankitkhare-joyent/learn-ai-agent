"""Agent #1: an agent with NO framework - the Claude API, an MCP client, and a while-loop.

    mise exec -- python -m agents.raw_loop "tell me about instance i-0abc..., which region is it in and what is the status"

Read this file first. LangGraph and Strands do exactly this underneath; once you've seen the loop,
the frameworks are just structure, persistence and observability around it:

    messages = [user question]
    loop:
        response = LLM(system, tools, messages)
        if the model didn't ask for a tool: done, print the answer
        run every tool call it asked for (in parallel), append the results, go round again

The MCP part is only "where the tools come from": list_tools() gives names + JSON schemas the model
reads, call_tool() runs them on the MCP server.
"""
import asyncio
import json
import time

import anthropic
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agents.common import (
    MAX_STEPS,
    MCP_URL,
    MODEL,
    SYSTEM_PROMPT,
    mcp_access_token,
    question_from_argv,
    require_anthropic_key,
)


def to_claude_tool(t) -> dict:
    """MCP tool definition -> Claude API tool definition. They're nearly the same shape on purpose."""
    return {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}


async def run_tool(session: ClientSession, block) -> dict:
    t0 = time.perf_counter()
    result = await session.call_tool(block.name, block.input)
    # Prefer structured output (JSON) when the server provides it; it's compact and unambiguous.
    text = json.dumps(result.structuredContent) if result.structuredContent is not None else \
        "\n".join(c.text for c in result.content if c.type == "text")
    print(f"  -> {block.name}({json.dumps(block.input)}) "
          f"{'ERROR ' if result.isError else ''}{time.perf_counter() - t0:.2f}s, {len(text)} chars")
    return {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": bool(result.isError)}


async def ask(question: str) -> str:
    require_anthropic_key()
    client = anthropic.AsyncAnthropic()
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {mcp_access_token()}"}, timeout=30)
    async with http, streamable_http_client(MCP_URL, http_client=http) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        tools = [to_claude_tool(t) for t in (await session.list_tools()).tools]
        messages = [{"role": "user", "content": question}]
        usage = {"input": 0, "output": 0, "cache_read": 0}

        for step in range(1, MAX_STEPS + 1):
            response = await client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},  # tools + system + history are re-sent every step: cache them
            )
            usage["input"] += response.usage.input_tokens
            usage["output"] += response.usage.output_tokens
            usage["cache_read"] += response.usage.cache_read_input_tokens or 0

            if response.stop_reason == "refusal":
                return "The model declined this request."
            # Keep the whole content (thinking + tool_use blocks), not just text: the API needs it next turn.
            messages.append({"role": "assistant", "content": response.content})
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if not tool_calls:
                print(f"[{step} steps, tokens in={usage['input']} out={usage['output']} "
                      f"cache_read={usage['cache_read']}]")
                return "".join(b.text for b in response.content if b.type == "text")

            print(f"step {step}: model requested {len(tool_calls)} tool call(s)")
            # Parallel tool calls: run concurrently, return ALL results in ONE user message.
            results = await asyncio.gather(*(run_tool(session, b) for b in tool_calls))
            messages.append({"role": "user", "content": list(results)})

        return f"Stopped after {MAX_STEPS} steps without a final answer (raise AGENT_MAX_STEPS if this is legitimate)."


if __name__ == "__main__":
    q = question_from_argv("Which account has the most running vCPUs right now, and what is its largest running "
                           "instance?")
    print(asyncio.run(ask(q)))
