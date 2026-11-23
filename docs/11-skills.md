# 11 · Agent Skills: what they are and how an agent picks one

## What a skill is

A **skill is a folder of instructions (and optionally scripts, templates, reference docs) that teaches an agent how to
do one kind of task**, loaded only when needed. The required file is `SKILL.md`:

```
skills/ec2-fleet-report/
├── SKILL.md              # YAML front matter (name, description) + markdown instructions
└── report-template.md    # supporting file, read only if the instructions need it
```

```markdown
---
name: ec2-fleet-report
description: Write a fleet report for one AWS account or the whole EC2 inventory ... Use when the user asks for a
  report, review, summary or health check of EC2 instances ...
---
# EC2 fleet report
1. Call fleet_summary twice ... 2. Call find_instances ...
```

Anthropic released Agent Skills as an **open standard** in December 2025; by 2026 40+ products read the same format
(Claude apps and Claude Code, the Claude API, the Agent SDK, Codex-style CLIs, Snowflake, and others).

## How the agent knows which skill to use: progressive disclosure

| Layer | When it's in the context window | Size |
|---|---|---|
| 1. **Metadata**: `name` + `description` of *every* skill | always (system prompt) | ~100 tokens per skill |
| 2. **Instructions**: the body of `SKILL.md` | only after the model decides the skill matches | up to a few thousand tokens |
| 3. **Resources**: templates, references, scripts | only if the instructions call for them (scripts can be *run* without being read) | unbounded |

The selection itself is just the model reading the catalog: the user's request is compared (by the model, in its
reasoning) to each description, and it calls the "load skill" mechanism for the best match. There's no embedding search
or classifier involved in the standard design. So:

- **The description is the trigger.** Say *what it does* and *when to use it*, with the words users actually use
  ("report", "review", "health check"). A vague description = a skill that never loads (or loads for the wrong task).
- **Keep SKILL.md short and procedural**; push detail into referenced files the model opens only when needed.
- Hundreds of skills cost little, because only layer 1 is always loaded.

## See it happen

[agents/skills_agent.py](../agents/skills_agent.py) implements the mechanism by hand so nothing is hidden:

1. it reads only the front matter of `skills/*/SKILL.md` and appends the catalog to the system prompt;
2. it adds two tools, `load_skill(name)` and `read_skill_file(name, file)` (with a path-traversal check);
3. the trace shows when the model loads the skill and how many characters enter the context.

```bash
mise run agent:skills "give me a fleet report for account 392918273645"   # should load ec2-fleet-report
mise run agent:skills "which region is i-0abc... in?"                      # should NOT load it
```

In Claude Code, put the same folder in `.claude/skills/` and it's picked up automatically; on the Claude API, skills are
uploaded and attached to a request with the code-execution container. Same file, three runtimes.

## Skills vs tools vs MCP vs prompts

| | Is | Gives the agent | Example |
|---|---|---|---|
| **Tool** | a function the model can call | an *ability* (do/fetch something) | `find_instances` |
| **MCP server** | a standard way to ship tools/resources/prompts over a protocol | *abilities from another system* | the ec2-inventory server |
| **Skill** | packaged instructions + files, loaded on demand | *know-how*: how to combine abilities for a task, your conventions, templates | "how we write a fleet report" |
| **System prompt** | always-on instructions | identity, global rules | "always state the snapshot time" |

Rules of thumb: if it needs to *touch a system*, it's a tool (probably behind MCP). If it's *how your team does a task*
— steps, format, checklists, gotchas — it's a skill. Skills often *use* MCP tools, as this one does.

## Production notes

- Version skills in git with code review: a skill is a prompt that can change agent behavior across many tasks.
- Skills with scripts run code: treat third-party skills like third-party code (review, pin, sandbox).
- Evaluate: include "should trigger" and "should not trigger" prompts for each skill in your eval set.

### Sources
- [What are Agent Skills (SKILL.md structure)](https://atlan.com/know/ai-agent/ai-agent-skills/what-are-agent-skills/)
- [Agent Skills and progressive disclosure](https://www.newsletter.swirlai.com/p/agent-skills-progressive-disclosure)
- [Production-grade skills authoring guide 2026](https://noqta.tn/en/blog/production-grade-agent-skills-authoring-guide-2026)
- [Anthropic: Agent Skills docs](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
