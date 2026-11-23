#!/usr/bin/env python3
"""ETL: raw DescribeInstances Parquet (s3://raw) -> flattened Iceberg table `ec2.instances`.

This is the local stand-in for the Airflow + EMR-on-EKS Spark job. One run
processes one or more days. A day that is already in the table is written as an
*overwrite of that day's partition*, so re-running a day replaces it instead of
duplicating rows (idempotent, safe to retry - the property you want from every
Airflow task). New days are appended in weekly batches during a backfill.

    python -m pipeline.load_iceberg                      # every day that has raw data
    python -m pipeline.load_iceberg --start 2026-09-30   # just one day onward

Tables:
  ec2.instances       one row per instance per 2-hourly snapshot (fact table)
  ref.instance_types  vCPU / memory / GPU per instance type (dimension table)
"""
import argparse
import time
from datetime import UTC, date, datetime, timedelta

import duckdb
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.sorting import SortField, SortOrder
from pyiceberg.transforms import DayTransform, IdentityTransform
from pyiceberg.types import DoubleType, IntegerType, MapType, NestedField, StringType, TimestamptzType

from pipeline.catalog import S3_ACCESS_KEY, S3_ENDPOINT, S3_REGION, S3_SECRET_KEY, get_catalog

RAW_BUCKET, RAW_PREFIX = "raw", "ec2/describe_instances"
TABLE = "ec2.instances"
REF_TABLE = "ref.instance_types"
BATCH_DAYS = 7

# ------------------------------------------------------------------ schemas
# Explicit field IDs and required/optional flags: the schema is a contract that
# the API, the MCP tools and the agent all depend on. Never let it be inferred.
INSTANCES_SCHEMA = Schema(
    NestedField(1, "snapshot_ts", TimestamptzType(), required=True, doc="When the describe-instances call ran"),
    NestedField(2, "account_id", StringType(), required=True),
    NestedField(3, "region", StringType(), required=True),
    NestedField(4, "availability_zone", StringType()),
    NestedField(5, "instance_id", StringType(), required=True),
    NestedField(6, "instance_type", StringType(), required=True),
    NestedField(7, "instance_family", StringType()),
    NestedField(8, "vcpus", IntegerType()),
    NestedField(9, "memory_gib", DoubleType()),
    NestedField(10, "gpus", IntegerType()),
    NestedField(11, "state", StringType(), required=True, doc="pending|running|stopping|stopped|shutting-down|terminated"),
    NestedField(12, "state_reason", StringType()),
    NestedField(13, "launch_time", TimestamptzType()),
    NestedField(14, "platform", StringType(), doc="Linux/UNIX or Windows"),
    NestedField(15, "architecture", StringType()),
    NestedField(16, "lifecycle", StringType(), doc="on-demand or spot"),
    NestedField(17, "private_ip", StringType()),
    NestedField(18, "public_ip", StringType()),
    NestedField(19, "vpc_id", StringType()),
    NestedField(20, "subnet_id", StringType()),
    NestedField(21, "image_id", StringType()),
    NestedField(22, "reservation_id", StringType()),
    NestedField(23, "name", StringType(), doc="Name tag"),
    NestedField(24, "team", StringType(), doc="Team tag"),
    NestedField(25, "service", StringType(), doc="Service tag"),
    NestedField(26, "cost_center", StringType(), doc="CostCenter tag"),
    NestedField(27, "tags", MapType(28, StringType(), 29, StringType(), value_required=False)),
    identifier_field_ids=[],
)
# day(snapshot_ts) keeps files per partition few and large; region/account are
# handled by the sort order (min/max stats let engines skip row groups).
INSTANCES_SPEC = PartitionSpec(PartitionField(source_id=1, field_id=1000, transform=DayTransform(), name="snapshot_day"))
INSTANCES_SORT = SortOrder(SortField(source_id=3, transform=IdentityTransform()),
                           SortField(source_id=2, transform=IdentityTransform()),
                           SortField(source_id=5, transform=IdentityTransform()))
TABLE_PROPS = {
    "write.parquet.compression-codec": "zstd",
    "write.target-file-size-bytes": str(128 * 1024 * 1024),
    "commit.retry.num-retries": "5",
}

REF_SCHEMA = Schema(
    NestedField(1, "instance_type", StringType(), required=True),
    NestedField(2, "instance_family", StringType(), required=True),
    NestedField(3, "vcpus", IntegerType(), required=True),
    NestedField(4, "memory_gib", DoubleType(), required=True),
    NestedField(5, "gpus", IntegerType(), required=True),
    NestedField(6, "architecture", StringType(), required=True),
)
# (vcpus, memory GiB, gpus) - public AWS specs for the types the generator uses.
INSTANCE_TYPES = {
    "t3.micro": (2, 1, 0), "t3.xlarge": (4, 16, 0),
    "m5.large": (2, 8, 0), "m5.xlarge": (4, 16, 0),
    "m6g.xlarge": (4, 16, 0), "m6i.xlarge": (4, 16, 0), "m6i.2xlarge": (8, 32, 0), "m6i.4xlarge": (16, 64, 0),
    "m7i.large": (2, 8, 0), "m7i.xlarge": (4, 16, 0), "m7i.2xlarge": (8, 32, 0),
    "c7a.2xlarge": (8, 16, 0), "c7a.4xlarge": (16, 32, 0), "c7g.4xlarge": (16, 32, 0),
    "c7i.xlarge": (4, 8, 0), "c7i.2xlarge": (8, 16, 0),
    "r6i.large": (2, 16, 0), "r6g.4xlarge": (16, 128, 0), "r7g.2xlarge": (8, 64, 0), "r7g.4xlarge": (16, 128, 0),
    "i4g.4xlarge": (16, 128, 0), "i4i.2xlarge": (8, 64, 0),
    "g5.2xlarge": (8, 32, 1), "g5.12xlarge": (48, 192, 4), "p4d.24xlarge": (96, 1152, 8),
}


def ref_table_arrow() -> pa.Table:
    rows = []
    for itype, (vcpus, mem, gpus) in sorted(INSTANCE_TYPES.items()):
        family = itype.split(".")[0]
        arm = family[-1] == "g" and not family.startswith("g")
        rows.append({"instance_type": itype, "instance_family": family, "vcpus": vcpus, "memory_gib": float(mem),
                     "gpus": gpus, "architecture": "arm64" if arm else "x86_64"})
    return pa.Table.from_pylist(rows, schema=REF_SCHEMA.as_arrow())


# ------------------------------------------------------------------ transform
FLATTEN_SQL = """
WITH inst AS (
    SELECT r.snapshot_ts, r.region, r.OwnerId AS account_id, r.ReservationId AS reservation_id, unnest(r.Instances) AS i
    FROM raw r
), tagged AS (
    SELECT *, map_from_entries(list_transform(i.Tags, t -> {'k': t.Key, 'v': t.Value})) AS tags FROM inst
)
SELECT
    snapshot_ts,
    account_id,
    region,
    i.Placement.AvailabilityZone                       AS availability_zone,
    i.InstanceId                                       AS instance_id,
    i.InstanceType                                     AS instance_type,
    ref.instance_family,
    ref.vcpus,
    ref.memory_gib,
    ref.gpus,
    i.State.Name                                       AS state,
    i.StateReason.Code                                 AS state_reason,
    CAST(i.LaunchTime AS TIMESTAMPTZ)                  AS launch_time,
    i.PlatformDetails                                  AS platform,
    i.Architecture                                     AS architecture,
    coalesce(i.InstanceLifecycle, 'on-demand')         AS lifecycle,
    i.PrivateIpAddress                                 AS private_ip,
    i.PublicIpAddress                                  AS public_ip,
    i.VpcId                                            AS vpc_id,
    i.SubnetId                                         AS subnet_id,
    i.ImageId                                          AS image_id,
    reservation_id,
    tags['Name']                                       AS name,
    tags['Team']                                       AS team,
    tags['Service']                                    AS service,
    tags['CostCenter']                                 AS cost_center,
    i.Tags                                             AS tags
FROM tagged LEFT JOIN ref ON ref.instance_type = tagged.i.InstanceType
ORDER BY region, account_id, instance_id
"""


def s3fs():
    host = S3_ENDPOINT.split("://", 1)[1]
    return pafs.S3FileSystem(access_key=S3_ACCESS_KEY, secret_key=S3_SECRET_KEY, region=S3_REGION,
                             endpoint_override=host, scheme="http", request_timeout=60,
                             retry_strategy=pafs.AwsStandardS3RetryStrategy(max_attempts=10))


def raw_days(fs) -> list[date]:
    """Every dt=YYYY-MM-DD partition that exists in the raw bucket (any region)."""
    days = set()
    for region_dir in fs.get_file_info(pafs.FileSelector(f"{RAW_BUCKET}/{RAW_PREFIX}")):
        for dt_dir in fs.get_file_info(pafs.FileSelector(region_dir.path)):
            days.add(date.fromisoformat(dt_dir.base_name.removeprefix("dt=")))
    return sorted(days)


def read_raw_day(fs, day: date) -> pa.Table:
    tables = []
    for region_dir in fs.get_file_info(pafs.FileSelector(f"{RAW_BUCKET}/{RAW_PREFIX}")):
        region = region_dir.base_name.removeprefix("region=")
        sel = pafs.FileSelector(f"{region_dir.path}/dt={day.isoformat()}", allow_not_found=True, recursive=True)
        for f in fs.get_file_info(sel):
            if f.type != pafs.FileType.File:
                continue
            hour = int(f.path.split("hour=")[1].split("/")[0])
            t = pq.read_table(f.path, filesystem=fs)
            n = t.num_rows
            t = t.append_column("region", pa.array([region] * n, pa.string()))
            ts = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            t = t.append_column("snapshot_ts", pa.array([ts] * n, pa.timestamp("us", tz="UTC")))
            tables.append(t)
    return pa.concat_tables(tables) if tables else None


def transform(con, raw: pa.Table) -> pa.Table:
    con.register("raw", raw)
    try:
        # Materialize before unregistering: DuckDB's .arrow() result is a lazy stream.
        out = con.sql(FLATTEN_SQL).to_arrow_table()
    finally:
        con.unregister("raw")
    # list<struct<Key, Value>> -> map<string, string> (the Iceberg map type)
    tags = out.column("tags").combine_chunks()
    entries = tags.flatten()
    tag_map = pa.MapArray.from_arrays(tags.offsets, entries.field("Key"), entries.field("Value"))
    out = out.set_column(out.schema.get_field_index("tags"), "tags", tag_map)
    return out.cast(INSTANCES_SCHEMA.as_arrow())


# ------------------------------------------------------------------ load
def ensure_tables(catalog):
    for ns in ("ec2", "ref"):
        try:
            catalog.create_namespace(ns)
        except NamespaceAlreadyExistsError:
            pass
    try:
        table = catalog.load_table(TABLE)
    except NoSuchTableError:
        table = catalog.create_table(TABLE, schema=INSTANCES_SCHEMA, partition_spec=INSTANCES_SPEC,
                                     sort_order=INSTANCES_SORT, properties=TABLE_PROPS)
    try:
        ref = catalog.load_table(REF_TABLE)
    except NoSuchTableError:
        ref = catalog.create_table(REF_TABLE, schema=REF_SCHEMA, properties=TABLE_PROPS)
    ref.overwrite(ref_table_arrow())
    return table


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    args = p.parse_args()

    fs = s3fs()
    catalog = get_catalog()
    table = ensure_tables(catalog)
    con = duckdb.connect()
    con.register("ref", ref_table_arrow())

    days = [d for d in raw_days(fs) if (not args.start or d >= args.start) and (not args.end or d <= args.end)]
    print(f"processing {len(days)} days: {days[0] if days else '-'} .. {days[-1] if days else '-'}", flush=True)
    # Days already in the table get replaced (overwrite of that day's partition: idempotent re-runs).
    # New days are appended in batches: one commit per BATCH_DAYS during a backfill instead of one per day, which
    # keeps snapshots/manifests few and avoids delete planning for partitions that can't contain data yet.
    # A brand-new table has no snapshot yet, and inspect.partitions() raises on that.
    present = set() if table.current_snapshot() is None else {
        p["snapshot_day"] for p in table.inspect.partitions().column("partition").to_pylist()}
    t0, total, pending = time.time(), 0, []

    def flush():
        nonlocal pending
        if pending:
            table.append(pa.concat_tables(pending), snapshot_properties={"etl.days": str(len(pending))})
            pending = []

    for day in days:
        raw = read_raw_day(fs, day)
        if raw is None:
            continue
        flat = transform(con, raw)
        if day in present:
            flush()
            day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
            day_filter = And(GreaterThanOrEqual("snapshot_ts", day_start.isoformat()),
                             LessThan("snapshot_ts", (day_start + timedelta(days=1)).isoformat()))
            table.overwrite(flat, overwrite_filter=day_filter, snapshot_properties={"etl.day": day.isoformat()})
        else:
            pending.append(flat)
            if len(pending) >= BATCH_DAYS:
                flush()
        total += flat.num_rows
        if day.day == 1 or day == days[-1]:
            print(f"{day}: {flat.num_rows} rows (total {total}, {time.time() - t0:.0f}s)", flush=True)
    flush()
    print(f"done: {total} rows in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
