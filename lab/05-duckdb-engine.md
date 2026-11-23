# Lab 05 · DuckDB: the query engine and the hot cache

One sitting, about 35 minutes. Background: [docs/04](../docs/04-engine-choice.md),
[docs/03](../docs/03-rest-api.md#design-choices-worth-copying). Code: [api/app/engine.py](../api/app/engine.py),
the queries in [api/app/main.py](../api/app/main.py), [lab/duck.py](duck.py).

The API answers every request with DuckDB, along one of two paths: an in-memory table called `latest`, or the
Iceberg table on S3. After this lab you can explain:

- what DuckDB reads from S3 for a query, and how to see it
- why `WHERE snapshot_ts ...` decides whether a query reads 1 file or 183
- why the API keeps only the latest snapshot in memory, with numbers

`lab:duck` sets up DuckDB the same way `Engine._attach()` does, from the host side. Answers to every **Predict** are
at the bottom; timings are from my laptop.

## Before you start

```bash
source lab/env.sh
vim +/'def _attach' api/app/engine.py     # compare with connect() in lab/duck.py
```

## 1. What does a query read?

`EXPLAIN ANALYZE` runs the query and reports S3 requests (`HTTPFS HTTP Stats`) and the files the Iceberg scan
opened.

**Predict (P1):** rank these three by bytes read from S3: counting all 2 million rows, counting one snapshot time,
and the largest `vcpus` in the whole table.

```bash
x() { mise run -q lab:duck -- "EXPLAIN ANALYZE $1" | grep -E 'in:|#GET|Total Time|Files Read'; echo; }
x "SELECT count(*) FROM lake.ec2.instances"
x "SELECT count(*) FROM lake.ec2.instances WHERE snapshot_ts = '2026-09-30 22:00:00'"
x "SELECT max(vcpus) FROM lake.ec2.instances"
```

## 2. The history path

`GET /v1/ec2/instances/{id}/history?days=30` runs this shape of query
(see `instance_history` in [main.py](../api/app/main.py)):

**Predict (P2):** how many files does it read with the 30-day filter, and without it?

```bash
I=i-ff794db48de5443ab
x "SELECT snapshot_ts, state FROM lake.ec2.instances
   WHERE instance_id = '$I' AND snapshot_ts >= TIMESTAMPTZ '2026-09-30 22:00:00+00' - INTERVAL 30 DAY"
x "SELECT snapshot_ts, state FROM lake.ec2.instances WHERE instance_id = '$I'"
```

## 3. The hot path

The `latest` table is built in `refresh()` with `CREATE OR REPLACE TABLE latest AS SELECT ... WHERE snapshot_ts = ?`.
Build it yourself and see what it costs:

```bash
mise run -q lab:duck -- \
  "CREATE TABLE latest AS SELECT * FROM lake.ec2.instances WHERE snapshot_ts = '2026-09-30 22:00:00'" \
  "SELECT count(*) AS n_rows FROM latest" \
  "SELECT memory_usage FROM pragma_database_size() WHERE database_name = 'memory'"
```

**Predict (P3):** the whole table is about 18 MB of Parquet. If the API cached *all* of it, how much memory would that
take, against the API's `duckdb_memory_limit` of 1 GB?

```bash
time mise run -q lab:duck -- \
  "CREATE TABLE everything AS SELECT * FROM lake.ec2.instances" \
  "SELECT memory_usage, memory_limit FROM pragma_database_size() WHERE database_name = 'memory'"
```

## Break it, then fix it

**Break 1: the memory limit.** Run the cache build with a 20 MB limit (the API's container has 2 GB; imagine a
smaller pod).

**Predict (P4):** does building `latest` (about 1,000 rows) still work? Does `everything`?

```bash
mise run -q lab:duck -- --memory-limit 20MB \
  "CREATE TABLE latest AS SELECT * FROM lake.ec2.instances WHERE snapshot_ts = '2026-09-30 22:00:00'" \
  "SELECT count(*) AS n_rows FROM latest"
mise run -q lab:duck -- --memory-limit 20MB "CREATE TABLE everything AS SELECT * FROM lake.ec2.instances" 2>&1 | head -1
```

**Break 2: time zones.** `lab:duck` sets `TimeZone = 'UTC'`. Run the same "everything since 30 September" query
with your laptop's zone.

**Predict (P5):** same count, or different? Which one is right?

```bash
mise run -q lab:duck -- \
  "SELECT count(*) AS utc FROM lake.ec2.instances WHERE snapshot_ts >= '2026-09-30'" \
  "SET TimeZone = 'America/Los_Angeles'" \
  "SELECT count(*) AS los_angeles FROM lake.ec2.instances WHERE snapshot_ts >= '2026-09-30'" \
  "SELECT max(snapshot_ts) AS latest_as_shown FROM lake.ec2.instances"
```

**Fix:** never compare a `timestamptz` column with a bare date string. Write `TIMESTAMPTZ '2026-09-30 00:00:00+00'`,
or pin the session zone as `lab:duck` and the API do. Find where the API gets its timestamps: they arrive as
`datetime` objects with a zone (Pydantic parses `snapshot_ts` in the query string), so the ambiguity never reaches
SQL.

## Check your predictions

**P1.** From my run:

| query | in | GETs | files read |
|---|---|---|---|
| `count(*)` | 869 KiB | 104 | none |
| one snapshot time | 889 KiB | 106 | 1 |
| `max(vcpus)` | 3.8 MiB | 470 | 183 |

`count(*)` reads **no data files**: the row counts are in the manifests. Even it costs about 104 GETs: the metadata
file, the manifest list and around 102 manifests, one per commit the ETL made. That's planning overhead, and it
grows with every commit until you compact manifests (`rewrite_manifests`). The one-snapshot query adds a single
data file thanks to the `day(snapshot_ts)` partition. `max(vcpus)` has no filter, so it reads every file.

**P2.** 31 files (2.0 MiB) with the filter, 183 (7.1 MiB) without. The filter on `snapshot_ts` prunes by partition
(30 days plus today). The `instance_id` filter doesn't skip files here: each day's file holds every instance. It
does skip inside a file with more than one row group, which is where the sort order earns its keep. The query
finds 45 rows for this instance (it lived 3.7 days); the API collapses them to the 3 rows where the state changed.

**P3.** The `latest` table is 997 rows and uses about 2.3 MiB. Loading everything took about 5 s and roughly 800 MiB
(786 to 844 MiB on my runs) of the `953.6 MiB` limit: 18 MB of compressed Parquet is over 40 times bigger in memory. That's
the reason for the design: ~95% of questions ask about *now*, so keep only *now* in memory and let history read
pruned files.

**P4.** `latest` still works (`997`). `everything` fails:
`OutOfMemoryException: Out of Memory Error: failed to allocate data of size 1.0 MiB (18.2 MiB/19.0 MiB used)`.
DuckDB can spill sorts and joins to disk, but an in-memory table has nowhere to spill. The API's version of this
failure is the hot cache growing past its limit: `refresh()` would raise, the background loop logs
`cache refresh failed` and the API keeps serving the *previous* snapshot. Look for that `except` in `engine.py`;
it's the right behaviour, as long as someone alerts on the log line.

**P5.** Different: `utc` = 11922, `los_angeles` = 8119. `'2026-09-30'` means midnight *in the session's zone*, which
is 07:00 UTC in Los Angeles, so the 00, 02, 04 and 06 UTC snapshots (3,803 rows) silently dropped out. The
latest snapshot is displayed as `2026-09-30 15:00:00-07`: the same instant as 22:00 UTC, shown differently. The
UTC count is the one you meant.

## What to remember

- `EXPLAIN ANALYZE` plus the HTTP stats tell you what a query really costs on object storage.
- Partition filters decide files; sort order and stats decide row groups; metadata (manifests) is a cost too.
- Compressed on disk is not the size in memory. Cache the small hot slice, not the table.

Next: [Lab 06 · The REST API](06-rest-api.md).
