# Lab 02 · S3 (SeaweedFS): what is actually on disk

One sitting, about 40 minutes. Background: [docs/02](../docs/02-local-stack.md#data). Code:
[pipeline/generate_raw.py](../pipeline/generate_raw.py), [pipeline/load_iceberg.py](../pipeline/load_iceberg.py),
[lab/duck.py](duck.py).

Everything the stack knows is in two S3 buckets. After this lab you can explain:

- how the raw landing zone and the Iceberg warehouse differ, file by file
- why the same day of data is about 27 times smaller once it's in Iceberg
- what an S3 request really is (a signed HTTP call) and the three ways it fails

Answers to every **Predict** are at the bottom.

## Before you start

```bash
source lab/env.sh
mise run -q lab:duck -- "SELECT 'duckdb ok' AS check"
```

`lab:duck` is DuckDB on your laptop with the S3 keys from OpenBao and the Iceberg catalog attached as `lake`
(each argument is one SQL statement). The first run downloads DuckDB's `iceberg` and `httpfs` extensions.

## 1. The buckets, from inside the storage server

`weed shell` is SeaweedFS's admin console. It runs inside the container, so it needs no S3 keys.

**Predict (P1):** `raw` holds 6 months of 2-hourly snapshots for 4 regions; `lakehouse` holds the same data as an
Iceberg table. How many objects in `raw`? Which bucket is bigger, and by roughly how much?

```bash
docker compose exec seaweedfs sh -c 'echo "s3.bucket.list" | weed shell 2>/dev/null'
docker compose exec seaweedfs sh -c \
  'echo "fs.ls /buckets/raw/ec2/describe_instances/region=us-east-1/dt=2026-09-30" | weed shell 2>/dev/null'
```

## 2. One raw file

**Predict (P2):** the raw file has no `region` column (the ETL adds it, see `read_raw_day()`). Will DuckDB's
`DESCRIBE` show one anyway? And how many rows does one file have: one per instance, or something else?

```bash
F="s3://raw/ec2/describe_instances/region=us-east-1/dt=2026-09-30/hour=22/describe_instances.parquet"
mise run -q lab:duck -- \
  "SELECT count(*) AS raw_files FROM glob('s3://raw/ec2/describe_instances/*/*/*/*.parquet')" \
  "SELECT column_name, left(column_type, 60) AS column_type FROM (DESCRIBE SELECT * FROM '$F')" \
  "SELECT count(*) AS reservations, sum(len(Instances)) AS instances FROM '$F'"
```

## 3. The same day, in Iceberg

**Predict (P3):** the ETL writes one day as one Parquet file. How many data files are in the warehouse, against
183 days of data? And how many files are in the 2026-09-30 folder?

```bash
mise run -q lab:duck -- \
  "SELECT count(*) AS data_files FROM glob('s3://lakehouse/warehouse/*/data/*/*.parquet')" \
  "SELECT count(*) AS metadata_files FROM glob('s3://lakehouse/warehouse/*/metadata/*')" \
  "SELECT split_part(file, '/', -1) AS file FROM glob('s3://lakehouse/warehouse/*/data/snapshot_day=2026-09-30/*')"
```

**Predict (P4):** count the rows of 2026-09-30 two ways: by reading every Parquet file in the day's folder, and by
asking the Iceberg table. Same number?

```bash
mise run -q lab:duck -- \
  "SELECT count(*) AS rows_in_folder FROM 's3://lakehouse/warehouse/*/data/snapshot_day=2026-09-30/*.parquet'" \
  "SELECT count(*) AS rows_in_table, count(DISTINCT snapshot_ts) AS snapshot_times
     FROM lake.ec2.instances WHERE snapshot_ts >= TIMESTAMPTZ '2026-09-30 00:00:00+00'" \
  "SELECT split_part(file_path, '/', -1) AS live_file
     FROM iceberg_metadata(lake.ec2.instances) WHERE file_path LIKE '%snapshot_day=2026-09-30/%'"
```

From here on, look only at the live file. `SET VARIABLE` keeps its path for the statements that follow.

**Predict (P5):** both formats are zstd-compressed Parquet. How many bytes does 2026-09-30 take in each?

```bash
LIVE="SET VARIABLE live = (SELECT list(file_path) FROM iceberg_metadata(lake.ec2.instances)
                           WHERE file_path LIKE '%snapshot_day=2026-09-30/%')"
mise run -q lab:duck -- "$LIVE" "
  SELECT 'raw (48 files)' AS what, sum(total_compressed_size) AS bytes
    FROM parquet_metadata('s3://raw/ec2/describe_instances/*/dt=2026-09-30/*/*.parquet')
  UNION ALL
  SELECT 'iceberg (1 live file)', sum(total_compressed_size) FROM parquet_metadata(getvariable('live'))"
```

Now look inside the Iceberg file: which columns cost the most, and what statistics each column carries.

```bash
mise run -q lab:duck -- "$LIVE" \
  "SELECT path_in_schema AS col, sum(total_compressed_size) AS compressed, sum(total_uncompressed_size) AS raw_bytes
     FROM parquet_metadata(getvariable('live')) GROUP BY ALL ORDER BY compressed DESC LIMIT 6" \
  "SELECT path_in_schema AS col, stats_min_value AS min, stats_max_value AS max
     FROM parquet_metadata(getvariable('live')) WHERE path_in_schema IN ('region', 'account_id', 'snapshot_ts')"
```

## 4. An S3 request is just signed HTTP

`s3curl` (in [env.sh](env.sh)) is `curl --aws-sigv4` with the keys from OpenBao.

```bash
s3curl / | head -c 300; echo
s3curl 'raw?list-type=2&max-keys=2&prefix=ec2%2Fdescribe_instances%2Fregion%3Dus-east-1%2Fdt%3D2026-09-30%2F' \
  | grep -oE '<Key>[^<]+</Key>|<Size>[0-9]+</Size>'
s3curl 'raw/ec2/describe_instances/region%3Dus-east-1/dt%3D2026-09-30/hour%3D22/describe_instances.parquet' -I \
  | grep -iE '^(HTTP|content-length|etag)'
```

## Break it, then fix it

Three requests for the same bucket listing. **Predict (P6):** which error code does each one get?

```bash
# a) no signature at all
curl -s 'localhost:9000/raw?list-type=2&max-keys=1'; echo
# b) the right access key, the wrong secret
curl -s --aws-sigv4 'aws:amz:us-east-1:s3' --user "$(bao_field ec2-lakehouse/s3 access_key):wrong" \
  'localhost:9000/raw?list-type=2&max-keys=1'; echo
# c) the right keys, but '=' written as-is in the object key
s3curl 'raw/ec2/describe_instances/region=us-east-1/dt=2026-09-30/hour=22/describe_instances.parquet' -I | head -1
```

**Fix:** (a) and (b) are fixed by the right keys, which only OpenBao has. For (c), write `=` as `%3D` (section 4
does). Then explain to yourself why (b) and (c) produce the *same* error, and why that makes (c) so hard to debug.

## Check your predictions

**P1.** `raw` is about 725 MB in 8,784 objects (4 regions × 183 days × 12 snapshots). `lakehouse` is about 21 MB:
roughly 35 times smaller. The `fs.ls` shows 12 `hour=` folders per day: Hive-style `key=value` paths.

**P2.** Yes. `DESCRIBE` lists `dt`, `hour` and `region` at the end: DuckDB reads them from the `key=value` folders in
the path (Hive partitioning). The file itself has the DescribeInstances shape: `Groups`, `Instances` (a list of
deeply nested structs), `OwnerId`, `RequesterId`, `ReservationId`. One row per *reservation*:
`8784` files, and the 22:00 us-east-1 file has `349` reservations holding `508` instances. That's why the ETL's
first step is `unnest(r.Instances)`.

**P3.** It depends on the history of your table. On the stack I built this on: about 192 data files for 183 days,
about 370 metadata files (one JSON per commit, plus an Avro manifest list and manifests per commit), and 7 files in
the 2026-09-30 folder, because that day had been rewritten several times and each rewrite wrote a new file without
deleting the old one. **Right after `mise run reset` and a fresh seed** you'll see exactly 183 data files and 1 file
in the folder: nothing has been rewritten yet. Come back to P3 and P4 after [Lab 04](04-etl-commits.md), which
rewrites that day for you.

**P4.** On a freshly seeded stack, yes: both give `11922`. After any rewrite, no. On my stack, reading the folder
gave `61604` rows while the table gave `11922` rows over `12` snapshot times, and `iceberg_metadata` named exactly one
live file for the day. The others are leftovers: an overwrite removes a file from the *table*, not from S3, until
someone runs snapshot expiry and orphan-file cleanup (Lab 03). This is the most important lesson of the lab: never
read an Iceberg table's folder directly (a Spark job, an Athena table over the path, `aws s3 cp`). Always go through
the table's metadata, or you'll count deleted and duplicate data.

**P5.**

```
raw (48 files)        │ 2737326
iceberg (1 live file) │  102309
```

About 27 times smaller for the same day. Three causes, in order of how much they matter (inferred, not measured
one by one): the ETL keeps 27 flat columns and drops most of the nested API response (block devices, network
interfaces, security groups); one file instead of 48 means one set of dictionaries and footers; and the rows are
sorted by region, account and instance, so the 12 snapshots of the same instance sit next to each other and compress
to almost nothing. The column list confirms it: `instance_id`, `launch_time` and `reservation_id` are the biggest
because they are the most unique; `tags` is 87 KB uncompressed but only 7 KB compressed. In the stats, `region` runs
from `ap-northeast-1` to `us-east-1` and `snapshot_ts` covers the whole day: the file has one row group, so its
min/max can't skip anything inside the day. On a bigger file with many row groups, the sort order is what makes
those ranges narrow.

**P6.** (a) `AccessDenied`; (b) `SignatureDoesNotMatch`; (c) `HTTP/1.1 403 Forbidden`, whose body is also
`SignatureDoesNotMatch`. SigV4 signs a *canonical* form of the request. S3 canonicalizes `=` in a path as `%3D`;
curl 8.x signs it as a literal `=`. Two different strings, two different signatures. The server can't tell a wrong
secret from a wrong canonical string, so it reports both the same way. When a signed request fails only for some
keys, suspect encoding before credentials. (boto3 and the AWS CLI encode correctly; this bites hand-written
clients.)

## What to remember

- Raw is for replay and audit; Iceberg is for queries. Keep both, and keep raw cheap (lifecycle it to Glacier).
- File count, sort order and column choice matter more than the codec.
- Files on S3 are not the table. The table is whatever the current metadata points to (next lab). Read through it.

Next: [Lab 03 · The Iceberg catalog](03-iceberg-catalog.md).
