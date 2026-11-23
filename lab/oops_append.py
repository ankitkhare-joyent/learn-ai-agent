"""Break the table on purpose: append the latest snapshot's rows a second time.

    mise run lab:oops

This is what a non-idempotent ETL does when Airflow retries a task whose commit already succeeded:
the same rows land twice. lab/04-etl-commits.md uses it, then repairs the table with the real ETL
(pipeline/load_iceberg.py overwrites a day's partition, so re-running it is the fix).
"""
from pyiceberg.catalog import load_catalog
from pyiceberg.io import load_file_io

from agents.secrets import read_secret


def main():
    s3 = read_secret("ec2-lakehouse/s3")
    catalog = load_catalog("lake", type="rest", uri="http://localhost:8181/catalog", warehouse="lakehouse",
                           **{"s3.access-key-id": s3["access_key"], "s3.secret-access-key": s3["secret_key"]})
    table = catalog.load_table("ec2.instances")
    # The catalog vends s3.endpoint=http://seaweedfs:8333, which only resolves inside Docker (lab 03).
    table.io = load_file_io({**table.io.properties, "s3.endpoint": "http://localhost:9000"}, table.metadata_location)

    rows = table.scan(row_filter="snapshot_ts == '2026-09-30T22:00:00+00:00'").to_arrow()
    print(f"appending {rows.num_rows} rows that are already in the table (snapshot_ts 2026-09-30 22:00 UTC)...")
    table.append(rows, snapshot_properties={"lab": "oops-duplicate-append"})
    print(f"committed snapshot {table.metadata.current_snapshot_id}")


if __name__ == "__main__":
    main()
