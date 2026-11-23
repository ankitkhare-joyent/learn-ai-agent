# Architecture

The full diagram with the numbered request trace is [architecture.html](architecture.html) (open it in a browser;
also published at https://claude.ai/artifact/LhZdspwRd18AChjiPyVGtK). This is the same picture as a Mermaid diagram, which renders in VS Code and
GitHub.

```mermaid
flowchart LR
  subgraph laptop[Your laptop]
    A[Agents<br/>raw loop · LangGraph · Strands · skills]
  end
  C[(Claude API<br/>claude-opus-5-5)]
  M[MCP server :8001<br/>4 read-only tools]
  subgraph serving[Serving]
    API[REST API :8000<br/>auth · hot cache · history path]
  end
  subgraph lake[Lakehouse]
    LK[Lakekeeper :8181<br/>Iceberg REST catalog]
    PG[(Postgres)]
    S3[(SeaweedFS S3<br/>raw · lakehouse)]
  end
  P[Seed pipeline<br/>generate_raw → load_iceberg]
  B[[OpenBao :8200<br/>secrets for everything]]

  A -- "1 question + tools" --> C
  C -- "2 tool_use" --> A
  A -- "3 call_tool · Bearer aud=ec2-mcp" --> M
  M -- "4 token exchange" --> API
  M -- "5 GET /v1/ec2/* · Bearer aud=ec2-api" --> API
  API -- "snapshot id every 60s" --> LK
  API -- "6 history: read day partitions" --> S3
  LK --- PG
  LK -- metadata --> S3
  P -- raw Parquet --> S3
  P -- commit --> LK
  B -. "AppRole → env at start" .-> API & M & P & S3
  B -. "dev token: API key, agent login" .-> A
```

1. The agent sends Claude the question and the tool definitions it listed from the MCP server.
2. Claude replies with a tool call (for "largest": `find_instances(sort_by="vcpus", descending=true)`).
3. The agent calls the MCP server with a token meant for the MCP server.
4. The MCP server exchanges it for an API token for the same user (no token passthrough).
5. The API checks scopes and answers from the in-memory latest snapshot.
6. History questions read only the needed Iceberg day partitions from S3.
