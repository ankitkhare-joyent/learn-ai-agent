"""Shared settings for the three agents: which model, which MCP server, and how to get a token for it.

Credentials, in order of preference:
  * Claude: workload identity federation (ANTHROPIC_FEDERATION_* env vars; the SDK handles it - no key at all),
    then ANTHROPIC_API_KEY from the environment (`.env`, loaded by mise), then OpenBao (secret/ec2-lakehouse/anthropic).
  * The inventory API user: OpenBao (secret/ec2-lakehouse/agent), or AGENT_USERNAME / AGENT_PASSWORD.
"""
import os
import sys

import httpx

from agents.secrets import read_secret

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:8001/mcp")
API_URL = os.environ.get("INVENTORY_API_URL", "http://localhost:8000")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))   # hard stop for runaway tool loops

SYSTEM_PROMPT = """You are an AWS EC2 inventory assistant for the platform team.

Answer questions about EC2 instances using the ec2-inventory tools. Rules:
- Use tools for every fact; never guess instance IDs, accounts, regions or states.
- Inventory data is a snapshot taken every 2 hours. Always say which snapshot time the answer is from.
- "Largest" means most vCPUs; mention memory too, and break ties by memory.
- If a tool returns an error, read it: fix your arguments and retry once, or explain the problem to the user.
- If the user lacks permission for something, say so plainly. Don't try to work around it.
- Keep answers short: a sentence or two, then a compact table if listing several instances."""


def require_anthropic_key() -> None:
    federated = os.environ.get("ANTHROPIC_FEDERATION_RULE_ID") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if federated or os.environ.get("ANTHROPIC_API_KEY"):
        return
    key = read_secret("ec2-lakehouse/anthropic").get("api_key")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key   # the SDKs read it from the environment
        return
    sys.exit("No Claude credentials. Either put ANTHROPIC_API_KEY in learn-ai-agent/.env (mise loads it), "
             "or store it in OpenBao with: mise run set-anthropic-key")


def mcp_access_token() -> str:
    """Log in as the agent's user and get a token whose audience is the MCP server (not the API).

    In production the user would sign in through your IdP (authorization code + PKCE) and the agent
    host would hold their token; this password grant just keeps the demo self-contained.
    """
    creds = read_secret("ec2-lakehouse/agent")
    r = httpx.post(f"{API_URL}/auth/token", data={
        "username": os.environ.get("AGENT_USERNAME") or creds.get("username", "agent"),
        "password": os.environ.get("AGENT_PASSWORD") or creds.get("password", ""),
        "audience": "ec2-mcp",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def question_from_argv(default: str) -> str:
    return " ".join(sys.argv[1:]) or default
