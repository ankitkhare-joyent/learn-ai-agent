# Lab 03 · The Iceberg catalog (Lakekeeper + Postgres): what a "table" is

One sitting, about 40 minutes. Background: [docs/02](../docs/02-local-stack.md),
[docs/01](../docs/01-production-review.md#copy-on-write-vs-merge-on-read-for-the-2-hour-job). Code:
[pipeline/catalog.py](../pipeline/catalog.py), [scripts/init_lakehouse.py](../scripts/init_lakehouse.py),
[lab/iceberg_scan.py](iceberg_scan.py).

Lab 02 showed files on S3. This lab shows what turns a pile of files into a table. After it you can explain:

- the chain from table name → metadata file → snapshot → manifest list → manifests → data files
- why the catalog only has to store one string per table, and why that makes commits atomic
- why a client that works inside Docker breaks on your laptop, even with the right keys

Answers to every **Predict** are at the bottom.

## Before you start

```bash
source lab/env.sh
curl -s localhost:8181/management/v1/info | jq -c '{version, bootstrapped, "authz-backend"}'
```

## 1. Talk to the catalog over plain HTTP

The Iceberg REST catalog is an open spec; AWS Glue, S3 Tables, Polaris and Lakekeeper all serve it. Every client
starts with `/config`:

```bash
curl -s "$CAT/config?warehouse=lakehouse" | jq '{overrides, defaults, endpoints: (.endpoints | length)}'
```

**Predict (P1):** the next calls need the warehouse ID that `/config` returned as `prefix`. What happens if you leave
it out?

```bash
curl -s "$CAT//namespaces" | jq -c .                      # no prefix
P=$(cat_prefix); echo "prefix=$P"
curl -s "$CAT/$P/namespaces" | jq -c .
curl -s "$CAT/$P/namespaces/ec2/tables" | jq -c .
```

## 2. Load the table

**Predict (P2):** `GET .../tables/instances` is the call every engine makes before reading. Does the response
contain data rows, file lists, or neither? How many snapshots does the table have?

```bash
curl -s "$CAT/$P/namespaces/ec2/tables/instances" > /tmp/instances.json
jq '{"metadata-location", config}' /tmp/instances.json
jq '.metadata | {"format-version", "current-snapshot-id", snapshots: (.snapshots | length),
     partition: ."partition-specs"[0].fields, sort: [."sort-orders"[0].fields[] | ."source-id"], properties}' \
  /tmp/instances.json
```

The current snapshot's summary is the cheapest way to answer "how big is the table" without reading data:

```bash
jq '.metadata as $m | $m.snapshots[] | select(."snapshot-id" == $m."current-snapshot-id") | .summary' /tmp/instances.json
```

## 3. Follow the pointer to S3

`metadata-location` is a file on S3. Fetch it yourself:

```bash
ML=$(jq -r '."metadata-location"' /tmp/instances.json)
s3curl "${ML#s3://}" | gunzip | jq '{"table-uuid", location, "current-snapshot-id", "last-sequence-number"}'
```

**Predict (P3):** the catalog runs on Postgres. How many rows does Postgres need to describe `ec2.instances`, and what
is in them?

```bash
docker compose exec postgres psql -U lakekeeper -d lakekeeper -c \
  "SELECT name, typ, right(metadata_location, 60) AS metadata_location FROM tabular"
```

Now go one level down: the manifests. DuckDB reads them for you.

```bash
mise run -q lab:duck -- \
  "SELECT count(*) AS snapshots FROM iceberg_snapshots(lake.ec2.instances)" \
  "SELECT sequence_number, snapshot_id, timestamp_ms, operation
     FROM iceberg_snapshots(lake.ec2.instances) ORDER BY sequence_number DESC LIMIT 3" \
  "SELECT count(*) AS live_data_files, sum(record_count) AS n_rows FROM iceberg_metadata(lake.ec2.instances)"
```

**Predict (P4):** Lab 02 counted the data files on S3. How many does the table consider live?

## Break it, then fix it

`lab:scan` reads the table with PyIceberg from your laptop, the same library the ETL and the API use inside Docker.

**Predict (P5):** it has the right S3 keys from OpenBao. Will it work?

```bash
mise run -q lab:scan 2>&1 | grep -vE '^\s+(File|~|\^)' | tail -6
```

The obvious fix is to tell PyIceberg which endpoint to use. **Predict (P6):** does a client-side setting win?

```bash
mise run -q lab:scan -- --s3-endpoint http://localhost:9000 2>&1 | grep -E 'endpoint|Error' | tail -2
```

The fix that works replaces the table's FileIO *after* the catalog has answered (see `--force-endpoint` in
[iceberg_scan.py](iceberg_scan.py)):

```bash
mise run -q lab:scan -- --force-endpoint http://localhost:9000
```

**Predict (P7):** `plan_files` has 183 data files to choose from. How many will it pick for one snapshot time, and
which part of the metadata lets it skip the rest without opening them?

## Check your predictions

**P1.** `{"error":{"message":"Provided WarehouseId is not a valid UUID. Got: {s}","type":"WarehouseIdIsNotUUID","code":400,...}}`.
The `/config` call is not a formality: it tells the client the prefix, the catalog URI (`overrides.uri`) and the
endpoints this server supports. With the prefix you get `{"namespaces":[["ec2"],["ref"]]}` and
`{"identifiers":[{"namespace":["ec2"],"name":"instances"}]}`.

**P2.** Neither rows nor file lists. You get the table's full *metadata*: schema, partition spec
(`day(snapshot_ts)` named `snapshot_day`), sort order (source IDs `[3, 2, 5]` = region, account_id, instance_id),
properties (`zstd`, 128 MB target files, 5 commit retries) and every snapshot (one per ETL commit, two more per
re-run of a day; 117 on my stack, fewer on a fresh seed), format version 2.
The current snapshot's summary says `total-records: 1996087`, `total-data-files: 183`, and `etl.days` or `etl.day`:
the ETL writes its own properties into each commit, which is how you'd answer "which job wrote this" in production.
Also note `config`: the catalog hands every client `"s3.endpoint": "http://seaweedfs:8333/"`. Keep that in mind.

**P3.** One row. The `tabular` table holds `instance_types` and `instances`, each with a `metadata_location` that ends
in `...gz.metadata.json`. Lakekeeper also keeps snapshots and schemas in other tables for fast listing, but the
source of truth for "what is this table right now" is that one string. A commit is a compare-and-swap of that
string: "set it to the new file, if it still points at the one I started from". That's why two writers can't
corrupt a table, and why `commit.retry.num-retries` exists.

**P4.** 183 live files and 1,996,087 rows. On a fresh seed S3 has the same 183; once a day has been rewritten (Lab
04) S3 has more (192 on my stack). The difference is files that an overwrite removed from
the table but nothing deleted from S3. In production you schedule `expire_snapshots` (drops old snapshots and their
now-unreferenced files) and `remove_orphan_files` (files no snapshot ever referenced, such as from failed writes).
Until then, time travel to old snapshots still works.

**P5.** No. `load_table` works (one HTTP call to the catalog) and prints
`S3 endpoint the client will use: http://seaweedfs:8333/`, then the scan fails with
`AWS Error NETWORK_CONNECTION during HeadObject operation: ... Could not resolve host: seaweedfs`. The catalog vended
an endpoint that only resolves inside the Docker network. The first S3 read is the manifest list (`snap-....avro`),
not a data file: planning needs S3 too.

**P6.** No: same error. PyIceberg merges the catalog's per-table `config` *over* the properties you passed to
`load_catalog`, so the server wins. That's deliberate: in production the catalog vends per-table credentials and
endpoints (`ACCESS_DELEGATION_MODE`), and a client shouldn't be able to override them by accident. DuckDB in
`lab:duck` works because it's attached with `ACCESS_DELEGATION_MODE 'none'` and uses its own S3 secret, exactly
like the API does in [engine.py](../api/app/engine.py).

**P7.** One file, `997 rows at 2026-09-30T22:00:00+00:00`. Each manifest stores every data file's partition value
(`snapshot_day`) and per-column min/max, so planning prunes 182 files from metadata alone. Planning still reads the
manifest list and the manifests themselves from S3 (0.02 to 0.6 s on my runs, depending on what was cached); Lab 05
counts those requests.

## What to remember

- A table is a pointer to a metadata file. Snapshots, manifests and data files are all immutable; a commit only
  writes new files and moves the pointer.
- The catalog's job is that one atomic pointer swap (plus access control and credential vending).
- Server-vended config beats client config. Know what your catalog hands out.

Next: [Lab 04 · Commits, snapshots and idempotent ETL](04-etl-commits.md).
