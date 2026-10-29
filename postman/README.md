# Postman MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
Postman API key on every request**; the server stores nothing.

This folder is standalone: it shares no code with other connectors in the repo.

## Quick start

Run in Command Prompt, inside this `postman` folder:

```cmd
uv sync --extra dev
uv run pytest -q
```
Expect `14 passed` in about 10 seconds. No Postman account is needed: the tests
start the real connector next to a fake Postman with two separate accounts and
check that callers never see or change each other's data.

## Run the connector

Run in Command Prompt, inside this `postman` folder:

```cmd
uv run python -m connectors.postman.server
```
It serves `http://0.0.0.0:8000/mcp`. Set `HOST=127.0.0.1` to keep it reachable
only from your own machine and `PORT` to change the port. Health check:
`GET /health` returns `ok`.

## What callers send

```json
{
  "transport": "streamable_http",
  "url": "https://<your-host>/mcp",
  "headers": { "X-Api-Key": "<postman-api-key>" }
}
```

- `Authorization: Bearer <postman-api-key>` is accepted instead of `X-Api-Key`.
- Add `"X-Postman-Region": "eu"` for accounts on Postman's EU servers. The
  caller can only choose between `https://api.postman.com` and
  `https://api.eu.postman.com`; they can never supply a URL.

## Tools

| Tool | What it does | Kind |
|---|---|---|
| `get_me` | Who the key belongs to, plus API usage | read-only |
| `list_workspaces` | Workspaces the key can see | read-only |
| `get_workspace` | One workspace with its collections and environments | read-only |
| `list_collections` | Collections (optionally in one workspace) | read-only |
| `get_collection` | A collection's requests: name, method, URL, folder | read-only |
| `list_environments` | Environments (optionally in one workspace) | read-only |
| `get_environment` | An environment's variables; **secret values are always masked** | read-only |
| `create_collection` | New collection from a list of requests | write |
| `create_environment` | New environment; chosen keys stored as secrets | write |
| `delete_collection` | Delete a collection | **destructive** |
| `delete_environment` | Delete an environment | **destructive** |

What a caller can do is limited by their own key's access in Postman.

## Get a Postman API key

1. Sign in at `https://go.postman.co` (a free account is fine).
2. Click your avatar (top right), then **Settings**, then **API keys**
   (or open `https://go.postman.co/settings/me/api-keys`).
3. Click **Generate API Key**, name it (for example `mcp-test`), and copy the key.
   It starts with `PMAK-` and is shown only once.

Never commit the key, paste it into chats, or put it in this folder.

## Try it as one caller

With the connector running in one Command Prompt window, run this in a second
window, inside this `postman` folder:

```cmd
set POSTMAN_API_KEY=PMAK-...
uv run python scripts/try_it.py
```
It prints the tool list, your username and your workspaces.

## End-to-end check: did it really happen in Postman?

`scripts/verify_e2e.py` does each action **through the connector**, then asks the
Postman API **directly** whether it happened:

1. creates a collection `MCP verify <random>` with two requests to Postman Echo,
   and confirms it exists;
2. creates an environment `MCP verify env <random>` with a secret, confirms it is
   stored as a secret, and checks the connector masks it;
3. pauses so you can see both in the Postman app (and screenshot them);
4. deletes both through the connector and confirms they are really gone.

With the connector running in one window, run this in a second window, inside
this `postman` folder:

```cmd
set POSTMAN_API_KEY=PMAK-...
uv run python scripts/verify_e2e.py
```
Expected last line: `Summary: 13 passed, 0 failed`. It uses about 25 Postman API
calls. Optional: `set POSTMAN_WORKSPACE_ID=<id>` to pick the workspace (default:
your first personal one), `set POSTMAN_REGION=eu` for EU accounts.

## In Docker

Run in Command Prompt, inside this `postman` folder:

```cmd
docker build -t mcp-postman .
docker run --rm -p 127.0.0.1:8000:8000 mcp-postman
```

## Rules this connector follows

1. The API key comes from the request headers, never from env vars or files.
2. Headers are read with `connector_core.auth` (`include_all=True` is needed to see `Authorization`).
3. A new HTTP client is built per call. No global clients, no caches shared across callers.
4. Stateless: `mcp.run(transport="http", stateless_http=True)`.
5. No header values in logs or error messages; secret environment values are masked.
6. `follow_redirects=False`, so a caller's key is never forwarded to a redirect target.
7. The Postman host is fixed (US or EU); IDs are checked before they go into a URL path.

`POSTMAN_API_BASE` exists only so the tests can point the connector at a fake Postman.

## Moving this into a repo

Copy the whole `postman/` folder and leave out `.venv`, `__pycache__`,
`.pytest_cache` and `*.egg-info`.

## Status (2026-10-29)

- 14 automated tests against a fake Postman with two accounts. Deliberately
  breaking caller isolation fails 8 of them; breaking secret masking fails 1.
- `scripts/verify_e2e.py` checked against the fake in healthy and broken modes.
- `scripts/verify_e2e.py` against a real Postman account: 13 of 13 checks pass, each
  create and delete confirmed directly in Postman, not only through the connector.
- Not yet tested through the workflow designer with an agent calling the tools.