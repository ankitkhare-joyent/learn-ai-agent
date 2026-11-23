"""Idempotent bootstrap: S3 buckets, Lakekeeper bootstrap, and the `lakehouse` warehouse.

Lakekeeper is not idempotent over HTTP (a second bootstrap or an existing
warehouse returns 400), so every step checks state first.
"""
import json
import os
import time
import urllib.error
import urllib.request

import pyarrow.fs as pafs

LK = os.environ.get("ICEBERG_REST_URI", "http://lakekeeper:8181/catalog").rsplit("/catalog", 1)[0]
S3 = os.environ.get("S3_ENDPOINT", "http://seaweedfs:8333")
KEY, SECRET = os.environ.get("S3_ACCESS_KEY", "s3admin"), os.environ.get("S3_SECRET_KEY", "s3admin-local-secret")
BUCKETS = ("raw", "lakehouse")


def http(method, path, body=None):
    req = urllib.request.Request(f"{LK}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    fs = pafs.S3FileSystem(access_key=KEY, secret_key=SECRET, endpoint_override=S3.split("://")[1], scheme="http",
                           region="us-east-1", allow_bucket_creation=True)
    for b in BUCKETS:
        for attempt in range(30):
            try:
                fs.create_dir(b)
                print(f"bucket {b}: ok")
                break
            except OSError as e:
                print(f"bucket {b}: retry ({e})")
                time.sleep(2)

    _, info = http("GET", "/management/v1/info")
    if isinstance(info, dict) and info.get("bootstrapped"):
        print("lakekeeper: already bootstrapped")
    else:
        code, out = http("POST", "/management/v1/bootstrap", {"accept-terms-of-use": True})
        print(f"lakekeeper bootstrap -> {code} {out}")
        assert 200 <= code < 300, out

    _, whs = http("GET", "/management/v1/warehouse")
    if any(w["name"] == "lakehouse" for w in whs.get("warehouses", [])):
        print("warehouse lakehouse: already exists")
        return
    code, out = http("POST", "/management/v1/warehouse", {
        "warehouse-name": "lakehouse",
        "storage-profile": {
            "type": "s3", "bucket": "lakehouse", "key-prefix": "warehouse", "endpoint": S3,
            "region": "us-east-1", "path-style-access": True, "flavor": "s3-compat",
            "sts-enabled": False, "remote-signing-enabled": False,
        },
        "storage-credential": {"type": "s3", "credential-type": "access-key",
                               "access-key-id": KEY, "secret-access-key": SECRET},
        "delete-profile": {"type": "hard"},
    })
    print(f"create warehouse -> {code} {out}")
    assert 200 <= code < 300, out


if __name__ == "__main__":
    main()
