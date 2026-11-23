# 04 · Is DuckDB the right engine for the API? Measured.

Is reading Iceberg with DuckDB directly a bad idea, and what's faster? I measured it. DuckDB is the best of the
embeddable options for this workload, but most of an API's speed comes from not re-reading the lake on every request:
keep the latest snapshot in memory and send only history queries to Iceberg.

## The benchmark

[scripts/bench_engines.py](../scripts/bench_engines.py), run with `mise run bench` inside the compose network
(so S3 and the catalog are reached the same way the API reaches them).

Data: `ec2.instances`, **1,996,087 rows**, 6 months of 2-hourly snapshots, day partitions, zstd. Latest snapshot:
997 rows. Each number is the median of 5 warm runs (3 for Q4) — an API process is always warm.

| Access path | Q1 latest: top-5 by vCPU in an account | Q2 latest: one instance | Q3 one instance, 30-day history | Q4 6-month aggregate |
|---|---:|---:|---:|---:|
| DuckDB `iceberg` extension, query per request | 39 ms | 43 ms | **46 ms** | **87 ms** |
| PyIceberg scan → Arrow | 24 ms | 27 ms | 124 ms | 1,717 ms |
| Polars `scan_iceberg` | 35 ms | 17 ms | 93 ms | 510 ms |
| **Hot cache** (latest snapshot in DuckDB memory; Q3/Q4 via DuckDB-Iceberg) | **3.8 ms** | **2.5 ms** | 46 ms | 87 ms |

Measured on a MacBook (Docker VM 8 vCPU / 12 GB) **while other labs' containers were also running**, so absolute
numbers are noisy; the ratios are what matter.

## What the numbers say

1. **For "now" questions, the hot cache wins by ~10×** (2-4 ms vs 17-43 ms). Every engine that goes to the lake
   pays fixed costs per request: a catalog call for table metadata, manifest reads, S3 GETs. The latest snapshot is
   ~1k rows that change every 2 hours, so it belongs in memory. This is what your RayService API should add.
2. **For history and analytics, DuckDB is clearly fastest** (Q3 2-3×, Q4 6-20× faster than the alternatives):
   vectorized, multi-threaded execution, partition pruning and min/max skipping, and (since DuckDB 1.3) an in-memory
   cache of remote file blocks, so warm queries rarely touch S3 again.
3. **PyIceberg alone** is fine for tiny filtered reads but slow for anything that scans (it's a planner + reader,
   not a query engine). **Polars** sits between.
4. All of this depends on **file layout**. In an earlier benchmark of mine, a small-file layout was 5-13×
   slower than compacted files for *every* engine. Compaction is worth more than any engine swap.

## When to move off DuckDB

| Symptom | Move to |
|---|---|
| Many concurrent users running heavy ad-hoc analytics through the API | delegate those routes to **Trino** or **StarRocks** (shared, distributed, with caching) and keep DuckDB for the hot paths |
| Sub-10 ms point lookups at thousands of QPS, or lookups across all history by ID | a **serving store** fed by the ETL: Postgres, DynamoDB, StarRocks primary-key tables, ClickHouse |
| Dashboards that must be ~1 s at 5-month windows | **StarRocks materialized views** (in an earlier benchmark of mine: 1-2.6 s vs 10-27 s) |
| You're building your own query service in Rust | **DataFusion** (embeddable engine; Bauplan moved from DuckDB to it for control over planning) |

## Other notes for the production API

- Give each DuckDB process `memory_limit` and `threads` explicitly; in Kubernetes, size the pod to match.
- Use one connection and a **cursor per request** (connections aren't thread-safe; cursors are cheap).
- Refresh the hot cache by **polling the table's current snapshot ID** (a cheap catalog call); reload only when it
  changes, and swap atomically (`CREATE OR REPLACE TABLE` in a transaction). Keep heavy Python work (like PyIceberg's
  `inspect.partitions()`, which held the GIL for 25 s here and stalled requests) out of the serving process.
- With several replicas, each holds its own cache — fine at this size (a few MB); for larger hot sets, pre-build a
  small DuckDB/Parquet file per snapshot in the ETL and have replicas download it.

### Sources
- [DuckDB Iceberg extension](https://duckdb.org/docs/stable/core_extensions/iceberg/overview) · [DuckDB external file cache](https://duckdb.org/docs/stable/configuration/overview)
- [Single-node engines: DuckDB, DataFusion, Polars](https://iceberglakehouse.com/posts/2026-05-23-single-node-data-engineering-duckdb-datafusion-polars-lakesail/) · [Bauplan: DuckDB to DataFusion](https://bauplanlabs.com/post/duck-hunt-moving-bauplan-from-duckdb-to-datafusion)
