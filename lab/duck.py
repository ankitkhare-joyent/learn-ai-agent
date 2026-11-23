"""A DuckDB shell on your laptop, wired to the same lakehouse the API reads.

    mise run lab:duck "SELECT count(*) FROM lake.ec2.instances"
    mise run lab:duck "SELECT * FROM glob('s3://raw/ec2/describe_instances/region=us-east-1/dt=2026-09-30/*/*')"
    mise run lab:duck --memory-limit 20MB "SELECT ..."        # break things on purpose

It does what api/app/engine.py does at start-up, from the host side of Docker:
  * S3 keys come from OpenBao (your dev token), never from a file or the command line
  * S3 is localhost:9000 (the container name seaweedfs:8333 only resolves inside Docker)
  * the Iceberg REST catalog is localhost:8181, attached as `lake`
Each argument is one SQL statement; every statement's result is printed. EXPLAIN output is printed as plain text.
"""
import argparse
import os
import sys

import duckdb

from agents.secrets import read_secret


def connect(memory_limit: str = "1GB", s3_endpoint: str = "localhost:9000") -> duckdb.DuckDBPyConnection:
    s3 = read_secret("ec2-lakehouse/s3")
    if not s3:
        sys.exit("no S3 keys from OpenBao: is the stack up and is .bao/dev-token readable? (lab/01-openbao.md)")
    con = duckdb.connect(config={"memory_limit": memory_limit, "threads": 4})
    con.execute("SET TimeZone = 'UTC'")   # the data is UTC; without this DuckDB shows (and parses) your local time
    con.execute("INSTALL iceberg; INSTALL httpfs; LOAD iceberg; LOAD httpfs;")
    con.execute(f"""CREATE SECRET lake_s3 (TYPE s3, KEY_ID '{s3["access_key"]}', SECRET '{s3["secret_key"]}',
        ENDPOINT '{s3_endpoint}', URL_STYLE 'path', USE_SSL false, REGION 'us-east-1')""")
    con.execute("""ATTACH 'lakehouse' AS lake (TYPE iceberg, ENDPOINT 'http://localhost:8181/catalog',
        AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none')""")
    return con


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sql", nargs="+", help="SQL statements, one per argument")
    p.add_argument("--memory-limit", default="1GB")
    p.add_argument("--s3-endpoint", default=os.environ.get("LAB_S3_ENDPOINT", "localhost:9000"))
    args = p.parse_args()
    con = connect(args.memory_limit, args.s3_endpoint)
    for stmt in args.sql:
        try:
            rel = con.sql(stmt)
        except duckdb.Error as e:   # print the error like the DuckDB CLI does, not a Python traceback
            sys.exit(f"{type(e).__name__}: {e}")
        if rel is None:
            continue
        if stmt.lstrip().upper().startswith("EXPLAIN"):
            for row in rel.fetchall():
                print(row[-1])
        else:
            rel.show(max_width=200, max_rows=60)


if __name__ == "__main__":
    main()
