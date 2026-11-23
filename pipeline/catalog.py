"""Shared PyIceberg catalog + S3 settings for the pipeline, API and benchmarks.

Everything comes from environment variables so the same code runs in Docker
Compose, Kubernetes, or (with different values) against AWS Glue's Iceberg REST
endpoint.
"""
import os

from pyiceberg.catalog import load_catalog

CATALOG_URI = os.environ.get("ICEBERG_REST_URI", "http://lakekeeper:8181/catalog")
WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "lakehouse")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://seaweedfs:8333")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "s3admin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "s3admin-local-secret")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")


def s3_props() -> dict:
    return {
        "s3.endpoint": S3_ENDPOINT,
        "s3.access-key-id": S3_ACCESS_KEY,
        "s3.secret-access-key": S3_SECRET_KEY,
        "s3.region": S3_REGION,
        "s3.path-style-access": "true",
    }


def get_catalog():
    """Iceberg REST catalog (Lakekeeper locally; Glue/S3 Tables/Polaris in prod)."""
    return load_catalog("lake", **{"type": "rest", "uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_props()})
