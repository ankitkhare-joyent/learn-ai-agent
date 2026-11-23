"""Benchmark: ways a REST API can read the EC2 Iceberg table.

    mise run bench      (runs in a container on the compose network; S3 keys come from OpenBao)

Engines / access paths:
  duckdb_iceberg   DuckDB iceberg extension attached to the REST catalog, a fresh query per request
  pyiceberg_arrow  PyIceberg scan(row_filter) -> Arrow table -> filter/sort in Arrow (DuckDB on top)
  polars_iceberg   Polars scan_iceberg (lazy, predicate pushdown via PyIceberg)
  hot_cache        latest snapshot held in DuckDB memory, refreshed when Iceberg changes (what the API does)

Queries (the shapes the API / agent actually issue):
  Q1 latest snapshot: running instances in one account, top 5 by vCPU
  Q2 latest snapshot: one instance by ID
  Q3 history: one instance over the last 30 days
  Q4 analytics: running instances per region per day over all 6 months
"""
import statistics
import time
from datetime import timedelta

import duckdb
import polars as pl
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual

from pipeline.catalog import CATALOG_URI, S3_ACCESS_KEY, S3_ENDPOINT, S3_REGION, S3_SECRET_KEY, WAREHOUSE, get_catalog

RUNS = 5
ACCOUNT = "392918273645"


def timed(fn, runs=RUNS):
    fn()  # warm-up (metadata caches, connections) - an API process is always warm
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def main():
    catalog = get_catalog()
    table = catalog.load_table("ec2.instances")

    con = duckdb.connect()
    con.execute("LOAD iceberg; LOAD httpfs;")
    con.execute(f"""CREATE SECRET s3 (TYPE s3, KEY_ID '{S3_ACCESS_KEY}', SECRET '{S3_SECRET_KEY}',
                    ENDPOINT '{S3_ENDPOINT.split('://')[1]}', URL_STYLE 'path', USE_SSL false, REGION '{S3_REGION}')""")
    con.execute(f"""ATTACH '{WAREHOUSE}' AS lake (TYPE iceberg, ENDPOINT '{CATALOG_URI}',
                    AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none')""")
    # (DuckDB needs pytz to return TIMESTAMPTZ via fetchone(); going through Arrow avoids it)
    latest_ts = con.execute("SELECT max(snapshot_ts) AS ts FROM lake.ec2.instances").to_arrow_table()["ts"][0].as_py()
    since = latest_ts - timedelta(days=30)
    instance_id = con.execute("""SELECT instance_id FROM lake.ec2.instances WHERE snapshot_ts = ?
        AND service = 'kafka-broker' LIMIT 1""", [latest_ts]).fetchone()[0]
    con.execute("CREATE TABLE latest AS SELECT * FROM lake.ec2.instances WHERE snapshot_ts = ?", [latest_ts])

    q1 = f"""SELECT instance_id, instance_type, vcpus FROM {{src}} WHERE account_id = '{ACCOUNT}' AND state = 'running'
             {{ts}} ORDER BY vcpus DESC, memory_gib DESC LIMIT 5"""
    q2 = f"SELECT * FROM {{src}} WHERE instance_id = '{instance_id}' {{ts}}"
    ts_filter = f"AND snapshot_ts = '{latest_ts.isoformat()}'::TIMESTAMPTZ"

    results = {}

    # --- DuckDB over Iceberg, per request
    results["duckdb_iceberg"] = {
        "Q1": timed(lambda: con.execute(q1.format(src="lake.ec2.instances", ts=ts_filter)).to_arrow_table()),
        "Q2": timed(lambda: con.execute(q2.format(src="lake.ec2.instances", ts=ts_filter)).to_arrow_table()),
        "Q3": timed(lambda: con.execute("""SELECT snapshot_ts, state FROM lake.ec2.instances
                    WHERE instance_id = ? AND snapshot_ts >= ? ORDER BY 1""", [instance_id, since]).to_arrow_table()),
        "Q4": timed(lambda: con.execute("""SELECT date_trunc('day', snapshot_ts) d, region, count(*) FILTER
                    (WHERE state = 'running') / 12.0 FROM lake.ec2.instances GROUP BY ALL""").to_arrow_table(), runs=3),
    }

    # --- PyIceberg -> Arrow
    latest_filter = EqualTo("snapshot_ts", latest_ts.isoformat())

    def py_q1():
        t = table.scan(row_filter=And(latest_filter, EqualTo("account_id", ACCOUNT), EqualTo("state", "running")),
                       selected_fields=("instance_id", "instance_type", "vcpus", "memory_gib")).to_arrow()
        return t.sort_by([("vcpus", "descending"), ("memory_gib", "descending")]).slice(0, 5)

    def py_q4():
        t = table.scan(row_filter=EqualTo("state", "running"), selected_fields=("snapshot_ts", "region")).to_arrow()  # noqa: F841
        return duckdb.sql("SELECT date_trunc('day', snapshot_ts) d, region, count(*) FROM t GROUP BY ALL").to_arrow_table()

    results["pyiceberg_arrow"] = {
        "Q1": timed(py_q1),
        "Q2": timed(lambda: table.scan(row_filter=And(latest_filter, EqualTo("instance_id", instance_id))).to_arrow()),
        "Q3": timed(lambda: table.scan(row_filter=And(EqualTo("instance_id", instance_id),
                                                      GreaterThanOrEqual("snapshot_ts", since.isoformat())),
                                       selected_fields=("snapshot_ts", "state")).to_arrow()),
        "Q4": timed(py_q4, runs=3),
    }

    # --- Polars
    lf = pl.scan_iceberg(table, storage_options={
        "aws_access_key_id": S3_ACCESS_KEY, "aws_secret_access_key": S3_SECRET_KEY, "aws_endpoint_url": S3_ENDPOINT,
        "aws_region": S3_REGION, "aws_allow_http": "true", "aws_virtual_hosted_style_request": "false"})
    pl_ts = pl.lit(latest_ts).cast(pl.Datetime("us", "UTC"))
    results["polars_iceberg"] = {
        "Q1": timed(lambda: lf.filter((pl.col("snapshot_ts") == pl_ts) & (pl.col("account_id") == ACCOUNT)
                                      & (pl.col("state") == "running"))
                    .sort(["vcpus", "memory_gib"], descending=True).head(5).collect()),
        "Q2": timed(lambda: lf.filter((pl.col("snapshot_ts") == pl_ts) & (pl.col("instance_id") == instance_id))
                    .collect()),
        "Q3": timed(lambda: lf.filter((pl.col("instance_id") == instance_id)
                                      & (pl.col("snapshot_ts") >= pl.lit(since).cast(pl.Datetime("us", "UTC"))))
                    .select("snapshot_ts", "state").collect()),
        "Q4": timed(lambda: lf.filter(pl.col("state") == "running")
                    .group_by(pl.col("snapshot_ts").dt.truncate("1d"), "region").len().collect(), runs=3),
    }

    # --- Hot cache (latest snapshot in memory); history still goes to Iceberg
    results["hot_cache"] = {
        "Q1": timed(lambda: con.execute(q1.format(src="latest", ts="")).to_arrow_table()),
        "Q2": timed(lambda: con.execute(q2.format(src="latest", ts="")).to_arrow_table()),
        "Q3": results["duckdb_iceberg"]["Q3"],
        "Q4": results["duckdb_iceberg"]["Q4"],
    }

    rows = con.execute("SELECT count(*) FROM lake.ec2.instances").fetchone()[0]
    print(f"\nTable ec2.instances: {rows:,} rows; latest snapshot {latest_ts} "
          f"({con.execute('SELECT count(*) FROM latest').fetchone()[0]} rows). Median of {RUNS} warm runs (Q4: 3).\n")
    print("| engine | Q1 latest top-5 | Q2 latest by ID | Q3 30-day history | Q4 6-month aggregate |")
    print("|---|---|---|---|---|")
    for name, r in results.items():
        print(f"| {name} | " + " | ".join(f"{r[q] * 1000:,.1f} ms" for q in ("Q1", "Q2", "Q3", "Q4")) + " |")


if __name__ == "__main__":
    main()
