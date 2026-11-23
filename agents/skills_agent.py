"""Agent #4: how Agent Skills work, implemented in ~40 lines on top of the raw loop.

    mise run agent:skills "give me a fleet report for account 392918273645"

Progressive disclosure, step by step:
  1. At start-up, read only the YAML front matter (name + description) of every skills/*/SKILL.md.
  2. Put that catalog (a line per skill) in the system prompt. That's all the model knows up front.
  3. Give the model one extra tool, load_skill(name). When a task matches a description, the model calls it and
     gets the full SKILL.md body (instructions), plus the list of other files in the skill folder.
  4. It can then call read_skill_file(name, file) for templates/references only if it needs them.

The model decides *which* skill to load by matching the user's request against the descriptions - which is why the
description must say both what the skill does and when to use it.
"""
import asyncio
from pathlib import Path

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
from agents.raw_loop import run_tool, to_claude_tool

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def parse_skill(path: Path) -> tuple[dict, str]:
    """Split SKILL.md into (front matter dict, body). Front matter is simple `key: value` YAML."""
    text = path.read_text()
    _, front, body = text.split("---", 2)
    meta = dict(line.split(":", 1) for line in front.strip().splitlines())
    return {k.strip(): v.strip() for k, v in meta.items()}, body.strip()


def skill_catalog() -> dict[str, Path]:
    return {parse_skill(p)[0]["name"]: p.parent for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))}


SKILL_TOOLS = [
    {"name": "load_skill", "description": "Load the full instructions of a skill from the catalog in the system prompt.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "read_skill_file", "description": "Read a supporting file (template, reference) inside a loaded skill.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "file": {"type": "string"}},
                      "required": ["name", "file"]}},
]


def run_skill_tool(block, catalog: dict[str, Path]) -> dict:
    name = block.input.get("name", "")
    folder = catalog.get(name)
    if folder is None:
        content, err = f"unknown skill {name!r}; available: {sorted(catalog)}", True
    elif block.name == "load_skill":
        files = [p.name for p in folder.iterdir() if p.name != "SKILL.md"]
        content, err = parse_skill(folder / "SKILL.md")[1] + f"\n\n(Other files in this skill: {files})", False
        print(f"  -> load_skill({name})  [{len(content)} chars of instructions now in context]")
    else:
        target = (folder / block.input["file"]).resolve()
        ok = target.is_relative_to(folder) and target.is_file()     # no path traversal out of the skill folder
        content, err = (target.read_text(), False) if ok else (f"no such file in skill {name}", True)
        print(f"  -> read_skill_file({name}, {block.input['file']})")
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": err}


async def ask(question: str) -> str:
    require_anthropic_key()
    catalog = skill_catalog()
    listing = "\n".join(f"- {n}: {parse_skill(p / 'SKILL.md')[0]['description']}" for n, p in catalog.items())
    system = (f"{SYSTEM_PROMPT}\n\n## Skills\nThese skills are available. If the request matches a skill's "
              f"description, call load_skill first and follow its instructions.\n{listing}")
    print(f"[system prompt carries {len(catalog)} skill(s), {len(listing)} chars of metadata]")
    client = anthropic.AsyncAnthropic()
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {mcp_access_token()}"}, timeout=30)
    async with http, streamable_http_client(MCP_URL, http_client=http) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        tools = [to_claude_tool(t) for t in (await session.list_tools()).tools] + SKILL_TOOLS
        messages = [{"role": "user", "content": question}]
        for _ in range(MAX_STEPS):
            response = await client.messages.create(model=MODEL, max_tokens=16000, system=system, tools=tools,
                                                    messages=messages, output_config={"effort": "medium"},
                                                    cache_control={"type": "ephemeral"})
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                return "".join(b.text for b in response.content if b.type == "text")
            results = await asyncio.gather(*(
                asyncio.to_thread(run_skill_tool, b, catalog) if b.name in ("load_skill", "read_skill_file")
                else run_tool(session, b) for b in calls))
            messages.append({"role": "user", "content": list(results)})
        return "Stopped: step limit reached."


if __name__ == "__main__":
    print(asyncio.run(ask(question_from_argv("Give me a fleet report for account 392918273645"))))
