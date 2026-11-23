# 10 · mise (and uv): is it the modern way?

**Short answer: yes.** In 2026 the common, well-regarded setup for a Python project is **uv for Python packages** and
**mise for everything around it** (tool versions, env vars, tasks). This repo uses both.

## An analogy

| Old way | Modern replacement | What improved |
|---|---|---|
| `pip` + `venv` + `pip-tools` + `pyenv` | **uv** | one fast Rust binary: resolves + locks (`uv.lock`), creates venvs, installs Python itself, 10-100× faster |
| `asdf` / `nvm` / `pyenv` / `tfenv` + `direnv` + `Makefile` | **mise** | one fast Rust binary: per-project tool versions, env vars, and tasks, in one `mise.toml` |

mise (pronounced "meez", from *mise en place* — everything in its place before you cook) is the successor to asdf (it
reads `.tool-versions`) and a replacement for direnv and most Makefiles.

## The three things mise does — as used in [mise.toml](../mise.toml)

### 1. Dev tools
```toml
[tools]
python = "3.13.16"
uv = "0.12.22"
```
`cd learn-ai-agent` → exactly these versions are on PATH (installed on first use). Teammates and CI get the same.
`mise use python@3.13` writes the entry; `mise.lock` (optional) pins checksums.

### 2. Environment
```toml
[env]
_.file = ".env"                                    # load secrets/local overrides (git-ignored)
_.python.venv = { path = ".venv", create = true }  # auto-activate uv's venv
BAO_ADDR = "http://localhost:8200"
```
Replaces direnv: variables appear when you enter the folder and disappear when you leave.

### 3. Tasks
```toml
[tasks.up]
description = "Start OpenBao, S3, catalog, API and MCP server"
run = "docker compose up -d --build --wait api mcp"
```
`mise tasks` lists them, `mise run up`, `mise run agent:langgraph "question"`. Tasks run with the tools and env above,
support `depends`, parallelism, file `sources`/`outputs` for skipping up-to-date work. Replaces most Makefiles.

## Daily use

```bash
brew install mise
echo 'eval "$(mise activate zsh)"' >> ~/.zshrc      # shell integration (or use `mise exec -- cmd`)
cd learn-ai-agent && mise trust && mise install     # trust is a safety prompt for new config files
mise run sync                                       # uv sync --locked
mise run up && mise run seed
```

## How uv and mise split the work

- **uv** owns *Python dependencies*: `pyproject.toml` (with dependency **groups** per deployable here: `pipeline`,
  `api`, `agents`, `dev`) and `uv.lock`. Docker images install with `uv sync --locked --no-default-groups --group api`
  so each image gets only what it needs, reproducibly.
- **mise** owns *which uv and which Python*, the env, and the commands.
- Version conflicts are real, and the lockfile makes them visible early. Two you'll see in this repo:
  the agent frameworks pin the **MCP client v1** while the server uses **SDK v2** (so the server is a separate uv
  project, `mcp_server/`), and Strands pins the **Anthropic SDK 0.x** while 1.x is out.

## Is it "a good thing to use in this day and age"?

| | mise | Nix / Devbox | Dev containers | plain asdf / pyenv |
|---|---|---|---|---|
| Learning curve | low | high (Nix) / medium (Devbox) | medium | low |
| Reproducibility | good (pinned versions, lockfile) | **bit-for-bit** | good (image) | ok |
| Speed | very fast | slow first build | container start | slow shims |
| Env + tasks built in | **yes** | partial | via IDE | no |

mise gives "90% of Nix's value for 1% of the cost". Choose Nix when you need hermetic, bit-identical builds across a
large org; dev containers when everyone uses VS Code/Codespaces. For a data/platform engineer's repos, mise + uv is the
pragmatic default — and **CI uses the same file** (`jdx/mise-action` in GitHub Actions), so "works on my machine" and
CI agree.

### Sources
- [mise docs](https://mise.jdx.dev/) · [mise + Python/uv cookbook](https://mise.jdx.dev/mise-cookbook/python.html)
- [uv docs](https://docs.astral.sh/uv/)
- [Nix vs mise (2026)](https://www.oflight.co.jp/en/columns/nix-vs-mise-dev-env-comparison-2026)
