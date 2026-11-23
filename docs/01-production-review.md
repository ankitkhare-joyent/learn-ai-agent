# 01 · Writing a 2-hour inventory snapshot to Iceberg

The pipeline this lab models takes an EC2 `describe-instances` snapshot every two hours, lands it as Parquet,
and loads it into an Iceberg table that an API and an MCP server read from. This note covers the one design
question that comes up first with that shape: how each run should write, and how readers find the latest data.

## Copy-on-write vs merge-on-read for the 2-hour job

Copy-on-write (CoW) and merge-on-read (MoR) only matter for row-level changes: `MERGE`, `UPDATE`, `DELETE`, or
an overwrite that replaces some rows. CoW rewrites every data file that holds a changed row, so writes are slow and
reads stay simple. MoR writes small delete files next to the new rows, so writes are fast and every reader has to
merge them until compaction runs.

Inventory snapshots don't have row-level changes. Each run adds a new `snapshot_ts`, and nothing about the 08:00
snapshot changes at 10:00. So each run can be a plain `append`: one new small file, no deletes, no rewrite, and CoW
and MoR behave the same. A nightly job compacts the day's twelve small files into one.

The pattern to avoid is rewriting the whole day partition on every run, with `INSERT OVERWRITE` or a `MERGE`. That
writes 1+2+…+12 = 78 files' worth of data a day instead of 12, and each commit replaces files a reader may have
just planned against.

## Finding the latest snapshot

It's tempting to find the newest data by listing files or partitions. With Iceberg there's no need, because the
table's current snapshot lists exactly the live files, whatever the write mode. The API in this lab
([api/app/engine.py](../api/app/engine.py)):

- polls the table's current snapshot ID, which is one cheap catalog call;
- when it changes, reads the newest time with `SELECT max(snapshot_ts) … WHERE snapshot_ts >= <previous latest>`
  (or reads it from a snapshot summary property the ETL writes);
- queries `WHERE snapshot_ts = <that value>` and lets Iceberg pick the files.

Done this way, MoR is also safe for the API as long as the reader applies delete files. DuckDB's iceberg extension,
Trino and Spark all do; reading the Parquet paths directly doesn't.

MoR is worth it once you add a real upsert table, say the current state of each instance maintained with `MERGE`,
where rewriting large files every two hours gets expensive. Compact it nightly so readers rarely see many delete files.
