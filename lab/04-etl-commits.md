# Lab 04 · Writing to Iceberg: commits, snapshots and an idempotent ETL

One sitting, about 40 minutes. Background: [docs/01](../docs/01-production-review.md#copy-on-write-vs-merge-on-read-for-the-2-hour-job).
Code: [pipeline/load_iceberg.py](../pipeline/load_iceberg.py) (read `main()` first), [lab/oops_append.py](oops_append.py),
`refresh()` in [api/app/engine.py](../api/app/engine.py).

This is the only lab that changes data. It re-runs the ETL for one day, then breaks the table on purpose and repairs
it. You end with the same rows you started with. After it you can explain:

- what one ETL run does to the table, commit by commit
- how the API notices new data without anyone telling it
- why "re-run the job" is a safe repair only when the job is idempotent

Answers to every **Predict** are at the bottom. Snapshot IDs and sequence numbers in the answers are from my run;
yours will be different and higher.

## Before you start

```bash
source lab/env.sh
table_state
curl -s $API/readyz | jq -c .
```

Note the `current_snapshot` and `total_records`, and that the API's `iceberg_snapshot_id` is the same number.

## 1. Re-run yesterday's job

A typical production job runs every 2 hours and rewrites the day. Here, re-run the ETL for 2026-09-30 (the seed image,
using the `pipeline` AppRole from Lab 01):

**Predict (P1):** how many new snapshots will this create, with which operations? Will `total_records` change? Read the
`if day in present:` branch of `main()` first.

```bash
docker compose --profile seed run --rm --no-deps seed \
  python scripts/bao_exec.py python -m pipeline.load_iceberg --start 2026-09-30
table_state
```

**Predict (P2):** a reader that loaded the table between those two snapshots would see a whole day missing. Can that
happen? Check which snapshots were ever *current*:

```bash
curl -s "$CAT/$(cat_prefix)/namespaces/ec2/tables/instances" \
  | jq -c '.metadata | {snapshots: (.snapshots | length), ever_current: (."snapshot-log" | length)}'
```

## 2. Watch the API notice

The API polls the catalog every 60 s (`start_background_refresh`) and reloads its in-memory copy when the snapshot
ID changes. In a second tmux pane:

```bash
docker compose logs -f --since 1m api | grep --line-buffered '"hot cache'
```

and in the first:

```bash
for i in $(seq 1 15); do curl -s $API/readyz | jq -c '{iceberg_snapshot_id, rows}'; sleep 5; done
```

**Predict (P3):** how long until the ID changes? How many `hot cache loaded` lines will the logs show for one change?

## 3. Break it: a retry that isn't idempotent

`lab:oops` appends the 22:00 rows a second time, as if a job retried after its commit had already succeeded.

**Predict (P4):** the API serves the latest snapshot time. After the next refresh, what will `fleet_summary` by region
report for us-east-1 (it is 508 now)? Will anything error?

```bash
mise run -q lab:oops
table_state
sleep 65   # one refresh interval, plus the reload
curl -s $API/readyz | jq -c .
curl -s -H "Authorization: Bearer $(api_token)" "$API/v1/ec2/summary?group_by=region" | jq -c '.rows[] | {key, instances}'
```

Find the damage the way you would in production, with a query that should always return zero:

```bash
mise run -q lab:duck -- "SELECT count(*) AS n_rows, count(DISTINCT instance_id) AS instances
  FROM lake.ec2.instances WHERE snapshot_ts = '2026-09-30 22:00:00'"
```

## 4. Fix it

**Predict (P5):** re-run the same ETL command as in step 1. What will the `delete` commit remove this time: 11,922
rows like before, or something else?

```bash
docker compose --profile seed run --rm --no-deps seed \
  python scripts/bao_exec.py python -m pipeline.load_iceberg --start 2026-09-30
table_state
sleep 65; curl -s $API/readyz | jq -c .
```

The broken snapshot still exists. Time-travel to it (use the `current_snapshot` that `table_state` printed right
after `lab:oops`; in the `iceberg_snapshots` list below it's the `append` just before the newest `delete`):

```bash
mise run -q lab:duck -- "SELECT snapshot_id, operation FROM iceberg_snapshots(lake.ec2.instances)
  ORDER BY sequence_number DESC LIMIT 4"
OOPS=<the snapshot id of the lab:oops append>
mise run -q lab:duck -- "SELECT count(*) AS rows_at_oops FROM lake.ec2.instances AT (VERSION => $OOPS)
  WHERE snapshot_ts = '2026-09-30 22:00:00'"
```

## Check your predictions

**P1.** Two snapshots, one `delete` and one `append`, from one ETL run:

```
"#108 delete +0 -11922 total=1984165 by=2026-09-30",
"#109 append +11922 -0 total=1996087 by=2026-09-30"
```

`total_records` ends where it started (1,996,087). That's the overwrite path: PyIceberg's `overwrite(df,
overwrite_filter=day)` deletes the day's file and appends the new one. The run took about 4 seconds, most of it
container start-up.

**P2.** No. `snapshots` grows by two per run but `ever_current` only by one. Both snapshots are written in one
transaction and the catalog's pointer jumps straight from the old append to the new one, so the `delete` snapshot is
never current. That's the atomic commit from Lab 03 doing its job.

**P3.** Up to 60 s plus the reload (mine took 45 s; the reload itself took 0.17 s). You see **two** `hot cache loaded`
lines: uvicorn runs `--workers 2`, and each worker is a separate process with its own DuckDB and its own copy of the
cache. Each replica of a production API would reload too. That's fine while the latest snapshot is about 1,000 rows,
and worth knowing before it's a million.

**P4.** Nothing errors. `readyz` shows `rows: 1994`, and every region doubles: `us-east-1` 1016, `eu-central-1` 488,
`ap-northeast-1` 286, `ap-southeast-1` 204. An agent asked "how many instances in us-east-1?" would now answer 1016
with full confidence. The DuckDB check returns `1994` rows for `997` distinct instances. Silent wrong answers are
the most expensive failure in a data system; checks like this one belong in the pipeline (fail the job) or in the
API's `refresh()` (refuse to swap in a snapshot that fails them).

**P5.** It removes 12,919 rows: the day's 11,922 plus the 997 duplicates, because overwrite replaces *everything* in
the day's partition, whoever wrote it. Then it appends 11,922. `total_records` is back to 1,996,087, and after the
next refresh the API shows `rows: 997` again. Time travel to the `lab:oops` snapshot still shows `1994`. The table
keeps every snapshot until you expire them, which is also why S3 now has more files than the table uses.

## What to remember

- An Iceberg commit adds snapshots; it never edits files. "Undo" is either an overwrite or a rollback to a snapshot.
- Idempotent jobs (overwrite a partition, or append with a run ID you check first) make retries and repairs safe.
- Put the commit's origin in snapshot properties (`etl.day`, `etl.days`, here `lab`). That's how `table_state` can say
  who wrote each commit.

Next: [Lab 05 · DuckDB, the query engine](05-duckdb-engine.md).
