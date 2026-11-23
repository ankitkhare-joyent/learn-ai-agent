---
name: ec2-fleet-report
description: Write a fleet report for one AWS account or the whole EC2 inventory - size, capacity, largest instances, spot vs on-demand, and changes over the last N days. Use when the user asks for a report, review, summary or health check of EC2 instances, an account's fleet, or capacity. Requires the ec2-inventory MCP tools.
---

# EC2 fleet report

Produce a short, factual report from the ec2-inventory MCP tools. Never invent numbers: every figure comes from a tool result.

## Steps

1. Scope: if the user named an account, use it as `account_id` everywhere. Otherwise report on everything.
2. Call `fleet_summary` twice: `group_by="state"` and `group_by="region"` (with the account filter if any).
3. Call `find_instances(state="running", sort_by="vcpus", descending=true, limit=5)` for the largest instances.
4. Call `fleet_summary(group_by="lifecycle", state="running")` for spot vs on-demand.
5. If the user asked about changes, call `instance_history` only for the instances they mention (it is expensive).

## Output format

Use exactly this structure (see `report-template.md` in this folder for an example):

- **Title**: `EC2 fleet report - <account or "all accounts"> - as of <snapshot_ts UTC>`
- **Headline**: one sentence: running instances, total running vCPUs and memory.
- **By state** and **By region**: small tables.
- **Largest running instances**: table of instance_id, name, type, vCPUs, memory, region.
- **Spot share**: percentage of running vCPUs on spot.
- **Notes**: anything odd (stopped instances older than 21 days, no CostCenter tag), max 3 bullets.

## Rules

- State the snapshot time; data is refreshed every 2 hours.
- Round memory to whole GiB. Use thousands separators.
- If a tool returns "permission denied", say which part of the report could not be produced.
