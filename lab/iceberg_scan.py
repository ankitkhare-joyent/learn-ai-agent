"""Read the Iceberg table from your laptop with PyIceberg, the way pipeline/ and the API's refresh() do in Docker.

    mise run lab:scan                                             # uses the S3 endpoint the catalog hands out
    mise run lab:scan -- --s3-endpoint http://localhost:9000      # passes it to load_catalog(): does that win?
    mise run lab:scan -- --force-endpoint http://localhost:9000   # replaces the table's FileIO after load_table()

Prints what the catalog returned, then scans one snapshot time (only instance_id) and counts rows.
"""
import argparse
import time

from pyiceberg.catalog import load_catalog
from pyiceberg.io import load_file_io

from agents.secrets import read_secret


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--s3-endpoint", help="client-side s3.endpoint, passed to load_catalog()")
    p.add_argument("--force-endpoint", help="s3.endpoint set on the table's FileIO after load_table()")
    p.add_argument("--snapshot-ts", default="2026-09-30T22:00:00+00:00")
    args = p.parse_args()

    s3 = read_secret("ec2-lakehouse/s3")
    props = {"s3.access-key-id": s3.get("access_key"), "s3.secret-access-key": s3.get("secret_key")}
    if args.s3_endpoint:
        props["s3.endpoint"] = args.s3_endpoint
    catalog = load_catalog("lake", type="rest", uri="http://localhost:8181/catalog", warehouse="lakehouse", **props)

    t0 = time.perf_counter()
    table = catalog.load_table("ec2.instances")   # one REST call: metadata comes back in the response body
    print(f"load_table: {time.perf_counter() - t0:.2f}s  current snapshot {table.metadata.current_snapshot_id}")
    if args.force_endpoint:
        table.io = load_file_io({**table.io.properties, "s3.endpoint": args.force_endpoint}, table.metadata_location)
    print(f"  metadata file: {table.metadata_location}")
    print(f"  S3 endpoint the client will use: {table.io.properties.get('s3.endpoint')}")

    t0 = time.perf_counter()
    scan = table.scan(row_filter=f"snapshot_ts == '{args.snapshot_ts}'", selected_fields=("instance_id",))
    files = scan.plan_files()                     # reads the manifest list + manifests from S3, prunes partitions
    print(f"plan_files: {time.perf_counter() - t0:.2f}s  {len(files)} data file(s) to read")
    t0 = time.perf_counter()
    rows = scan.to_arrow().num_rows
    print(f"to_arrow: {time.perf_counter() - t0:.2f}s  {rows} rows at {args.snapshot_ts}")


if __name__ == "__main__":
    main()
