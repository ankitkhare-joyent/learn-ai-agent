# Build context is the repo root (so scripts/bao_exec.py can be copied in).
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY mcp_server/pyproject.toml mcp_server/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY mcp_server/server.py ./
COPY scripts/bao_exec.py ./
USER app
EXPOSE 8001
CMD ["python", "bao_exec.py", "python", "server.py", "--transport", "http"]
