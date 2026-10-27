# MCP connectors (FastMCP, HTTP, multi-tenant)

Each connector is a stateless FastMCP server over streamable HTTP. The caller
sends their own credentials in request headers; the server stores none.

## Quick start

```bash
uv sync --extra dev
uv run pytest -q        # 9 tests, about 10 seconds, no Slack account needed
```

The tests start the real connector next to a fake Slack and check that
concurrent callers never see each other's data.

## Run the Slack connector

```bash
uv run python -m connectors.slack.server   # serves http://0.0.0.0:8000/mcp
```

Set `HOST=127.0.0.1` to keep it reachable only from your own machine, and
`PORT` to change the port. Health check: `GET /health` returns `ok`.

Try it as one caller from a second terminal. Environment variables last only
for that terminal window, and the syntax differs by shell:

| Shell | Set the token |
|---|---|
| PowerShell (prompt starts with `PS`) | `$env:SLACK_TOKEN = "xoxb-..."` |
| Command Prompt (cmd) | `set SLACK_TOKEN=xoxb-...` (no quotes) |
| bash / zsh | `export SLACK_TOKEN=xoxb-...` |

Then: `uv run python scripts/try_it.py`

### In Docker

```bash
docker build --build-arg CONNECTOR=slack -t mcp-slack .
docker run --rm -p 127.0.0.1:8000:8000 mcp-slack
```

Register it in the workflow designer like any other HTTP connector:

```json
{
  "transport": "streamable_http",
  "url": "https://<your-host>/mcp",
  "headers": { "Authorization": "Bearer <slack-token>" }
}
```

## Rules every connector follows

1. **Credentials come from the request**, never from env vars or config files.
2. **Read headers with `connector_core.auth`**. FastMCP strips `Authorization`
   unless `include_all=True`, and the helper handles that.
3. **Build the API client per call** (`connector_core.http.make_client`). No
   global clients, no caches shared across callers.
4. **Run stateless**: `mcp.run(transport="http", stateless_http=True)`.
5. **Never put header values in logs or error messages.**
6. **If a caller can supply a base URL** (for example a self-hosted GitLab),
   validate it against an allowlist, or the server can be pointed at internal
   hosts (SSRF). Slack's base URL is fixed by the operator, so it needs none.
7. Mark read-only tools with `readOnlyHint`, writes with `destructiveHint`.

## Add a new connector

1. Copy `connectors/slack/` to `connectors/<name>/`.
2. Replace the API helper and tools; keep `/health` and `main()`.
3. Copy the Slack test file, point it at a fake of the new API, keep the
   concurrent-callers and missing-header tests.
4. Build: `docker build --build-arg CONNECTOR=<name> -t mcp-<name> .`

## Deploying

- The server is stateless, so it can run as several instances behind a load
  balancer. The host's `PORT` variable is respected.
- Decide who can reach the URL (public, gateway, or VPN). Anyone who can reach
  a connector can use it with their own credentials.
- Do not bake credentials into the image or environment. `SLACK_API_BASE`
  exists only so tests can point the connector at a fake Slack.

## Moving this into a repo

Copy only: `connector_core`, `connectors`, `tests`, `scripts`, `pyproject.toml`,
`uv.lock`, `README.md`, `Dockerfile`, `.dockerignore`, `.gitignore`. Do not copy
`.venv`, `__pycache__`, `.pytest_cache` or `*.egg-info`.

## Status (verified 2026-10-27)

- Automated suite against a fake Slack: 9 passing.
- Tools exercised against a real Slack workspace through MCP Inspector.
- Docker image built and run locally, then called with `scripts/try_it.py`.
