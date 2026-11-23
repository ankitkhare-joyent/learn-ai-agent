from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (12-factor). Secrets are SecretStr so they never get logged."""

    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore")

    # --- auth
    # RS256: the API signs with a private key (from OpenBao); anyone can verify with the public JWKS at
    # /.well-known/jwks.json - so the MCP server needs no shared secret.
    jwt_private_key: SecretStr
    jwt_issuer: str = "ec2-inventory-api"
    public_url: str = "http://localhost:8000"
    access_token_minutes: int = 30
    admin_username: str = "admin"
    admin_password: SecretStr
    # A service user for the agents (read + history). Created at startup if both are set.
    agent_username: str | None = None
    agent_password: SecretStr | None = None
    users_db: str = "/data/users.db"
    # The MCP server is a registered OAuth client allowed to do token exchange.
    mcp_client_id: str = "ec2-mcp"
    mcp_client_secret: SecretStr

    # --- data
    iceberg_rest_uri: str = Field("http://lakekeeper:8181/catalog", validation_alias="ICEBERG_REST_URI")
    iceberg_warehouse: str = Field("lakehouse", validation_alias="ICEBERG_WAREHOUSE")
    s3_endpoint_hostport: str = Field("seaweedfs:8333", validation_alias="S3_ENDPOINT_HOSTPORT")
    s3_access_key: SecretStr = Field(SecretStr("s3admin"), validation_alias="S3_ACCESS_KEY")
    s3_secret_key: SecretStr = Field(SecretStr("s3admin-local-secret"), validation_alias="S3_SECRET_KEY")
    s3_region: str = Field("us-east-1", validation_alias="S3_REGION")
    table: str = "ec2.instances"
    cache_refresh_seconds: int = 60
    max_page_size: int = 500
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
