"""Query engine: DuckDB over Iceberg, plus an in-memory "hot" copy of the latest snapshot.

Why two paths (docs/04-engine-choice.md has the measurements):
  * ~95% of API and agent calls ask about *now* ("what state is i-123 in?").
    Re-reading Parquet from S3 for each of those is pure waste: the latest
    snapshot is ~11k rows and changes every 2 hours. It lives in DuckDB memory and
    is swapped atomically when the Iceberg table gets a new snapshot.
  * History queries go to Iceberg through DuckDB's iceberg extension, with
    time filters so partition pruning reads only the days asked for.

DuckDB connections are not safe to share across threads; every request gets
its own cursor (cheap) on the one shared database.
"""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import duckdb
from pyiceberg.catalog import load_catalog

from .settings import Settings

log = logging.getLogger("api.engine")

COLUMNS = ("snapshot_ts, account_id, region, availability_zone, instance_id, instance_type, instance_family, vcpus, "
           "memory_gib, gpus, state, state_reason, launch_time, platform, architecture, lifecycle, private_ip, "
           "public_ip, vpc_id, subnet_id, image_id, name, team, service, cost_center")


@dataclass
class CacheInfo:
    iceberg_snapshot_id: int | None = None
    snapshot_ts: datetime | None = None
    rows: int = 0
    loaded_at: float | None = None
    load_seconds: float | None = None


class Engine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = duckdb.connect(config={"memory_limit": settings.duckdb_memory_limit,
                                         "threads": settings.duckdb_threads})
        self.cache = CacheInfo()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._catalog = load_catalog("lake", type="rest", uri=settings.iceberg_rest_uri,
                                     warehouse=settings.iceberg_warehouse, **{
                                         "s3.endpoint": f"http://{settings.s3_endpoint_hostport}",
                                         "s3.access-key-id": settings.s3_access_key.get_secret_value(),
                                         "s3.secret-access-key": settings.s3_secret_key.get_secret_value(),
                                         "s3.path-style-access": "true", "s3.region": settings.s3_region})
        self._attach()

    # ------------------------------------------------------------------ setup
    def _attach(self) -> None:
        s = self.s
        self.db.execute("LOAD iceberg; LOAD httpfs;")
        self.db.execute(f"""CREATE OR REPLACE SECRET lake_s3 (TYPE s3, KEY_ID '{s.s3_access_key.get_secret_value()}',
            SECRET '{s.s3_secret_key.get_secret_value()}', ENDPOINT '{s.s3_endpoint_hostport}',
            URL_STYLE 'path', USE_SSL false, REGION '{s.s3_region}')""")
        # Locally the catalog has no auth and doesn't vend credentials. Against AWS Glue's
        # Iceberg REST endpoint you'd use AUTHORIZATION_TYPE 'sigv4' and IAM (IRSA / Pod Identity).
        self.db.execute(f"""ATTACH IF NOT EXISTS '{s.iceberg_warehouse}' AS lake (TYPE iceberg,
            ENDPOINT '{s.iceberg_rest_uri}', AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none')""")

    @property
    def lake_table(self) -> str:
        return f"lake.{self.s.table}"

    # ------------------------------------------------------------------ hot cache
    def refresh(self, force: bool = False) -> bool:
        """Reload the latest snapshot into memory if the Iceberg table changed. Returns True if reloaded."""
        with self._refresh_lock:
            table = self._catalog.load_table(self.s.table)
            current = table.current_snapshot()
            if current is None:
                return False
            if not force and current.snapshot_id == self.cache.iceberg_snapshot_id:
                return False
            t0 = time.perf_counter()
            cur = self.db.cursor()
            # Newest snapshot time. After the first load, new data can only be at or after the cached one, so the
            # filter lets DuckDB prune to the last day or two of partitions. (An earlier version used PyIceberg's
            # inspect.partitions(): pure-Python manifest parsing that held the GIL for ~25 s and stalled requests.)
            since = self.cache.snapshot_ts
            where = "WHERE snapshot_ts >= ?" if since else ""
            latest_ts = cur.execute(f"SELECT max(snapshot_ts) AS ts FROM {self.lake_table} {where}",
                                    [since] if since else []).to_arrow_table().column("ts")[0].as_py()
            cur.execute("BEGIN")
            cur.execute(f"CREATE OR REPLACE TABLE latest AS SELECT {COLUMNS} FROM {self.lake_table} "
                        "WHERE snapshot_ts = ?", [latest_ts])
            cur.execute("COMMIT")
            rows = cur.execute("SELECT count(*) FROM latest").fetchone()[0]
            self.cache = CacheInfo(current.snapshot_id, latest_ts, rows, time.time(), time.perf_counter() - t0)
            log.info("hot cache loaded", extra={"snapshot_ts": str(latest_ts), "rows": rows,
                                                "seconds": round(self.cache.load_seconds, 3)})
            return True

    def start_background_refresh(self) -> None:
        def loop():
            while not self._stop.wait(self.s.cache_refresh_seconds):
                try:
                    self.refresh()
                except Exception:  # keep serving the old snapshot if the catalog hiccups
                    log.exception("cache refresh failed")
        threading.Thread(target=loop, name="cache-refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def ready(self) -> bool:
        return self.cache.snapshot_ts is not None

    # ------------------------------------------------------------------ queries
    def query(self, sql: str, params: list | None = None) -> list[dict]:
        # Arrow -> Python avoids DuckDB's pytz dependency for TIMESTAMPTZ and is fast.
        return self.db.cursor().execute(sql, params or []).to_arrow_table().to_pylist()
