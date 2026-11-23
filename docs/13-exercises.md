# 13 · Exercises (do them in order)

Each one builds a production habit. Solutions aren't provided on purpose; the tests and the agent traces tell you if
you got it right.

### MCP and tools
1. **Read the wire.** Run `agents/raw_loop.py` and print `messages` at the end. Find: the tool definitions the model
   saw, each `tool_use` block, each `tool_result`, the token usage per step. Where do most tokens go?
2. **Description matters.** Delete the "for largest use sort_by=vcpus" sentence from `find_instances` and ask
   "largest instance in account 392918273645" five times. Count wrong answers. Put it back.
3. **New tool.** Add `fleet_changes(account_id, hours=24)`: instances launched and terminated since then. Write the
   description first. Add a test in `tests/test_mcp.py`.
4. **Bounded output.** Make `find_instances` return 50 rows with all fields and watch tokens per answer grow. Then
   design a `fields` parameter instead.

### Agents
5. **Human approval.** Add a fake write tool `stop_instance(instance_id)` (it only logs) to a *second* MCP server, and
   a LangGraph `interrupt()` before it runs. Resume with approve/reject.
6. **Router node.** In LangGraph, add a first node that classifies the question (lookup / ranking / report) with a
   cheap single call and routes "report" to the skills flow.
7. **Strands multi-agent.** Make a "capacity analyst" agent that uses the inventory agent as a tool (agents-as-tools).

### Production
8. **Eval set.** Write 20 questions with expected tool calls and facts in `evals/cases.yaml` and a runner that scores
   tool selection + answer facts for all three agents. Run it before and after exercise 2.
9. **Tracing.** Turn on Strands' OpenTelemetry export to a local Jaeger/Phoenix and follow one question across agent →
   MCP → API.
10. **Least privilege.** Create a user with only `inventory:read`, get an `ec2-mcp` token for it, and ask the agent for
    an instance's history. Check the agent explains the permission problem instead of looping.
11. **Rotate a secret.** Change the agent password in OpenBao (`bao kv put` / UI), restart `api`, re-run an agent.
12. **Engine choice.** Re-run `mise run bench` after compacting the Iceberg table into fewer, larger files (rewrite the
    table with one append per month) and compare Q3/Q4.

### Your real platform
13. Write the MCP server you'd put in front of your RayService API: list the 4-6 tools, their inputs/outputs, scopes,
    and which ones need approval. Compare with your current "API as MCP tool" setup.
