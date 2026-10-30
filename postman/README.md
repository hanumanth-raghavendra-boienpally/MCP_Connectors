# Postman MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
Postman API key on every request**; the server stores nothing.

This folder is standalone: it shares no code with other connectors in the repo.

## Quick start

Needs **Python 3.12 or newer** and [`uv`](https://docs.astral.sh/uv/).

Run in Command Prompt, inside this `postman` folder:

```cmd
uv sync --extra dev
uv run pytest -q
```
Expect `21 passed` in about 10 seconds. No Postman account is needed: the tests
start the real connector next to a fake Postman with two separate accounts and
check that callers never see or change each other's data.

Without `uv` (classic pip), in PowerShell inside this `postman` folder:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python -m pytest -q
```

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
| `add_request` | Add one request to an existing collection; nothing else changes | write |
| `update_environment` | Change, add or remove variables in an existing environment; untouched variables (and secrets) stay as they are, values are never echoed | **destructive** (can remove variables) |
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
   confirms it exists, then adds a third request with `add_request` and confirms the
   first two are untouched;
2. creates an environment `MCP verify env <random>` with a secret, confirms it is
   stored as a secret, checks the connector masks it, then edits it with
   `update_environment` (one value changed, one added, one removed) and confirms the
   secret was kept;
3. pauses so you can see both in the Postman app (and screenshot them);
4. deletes both through the connector and confirms they are really gone.

With the connector running in one window, run this in a second window, inside
this `postman` folder:

```cmd
set POSTMAN_API_KEY=PMAK-...
uv run python scripts/verify_e2e.py
```
Expected last line: `Summary: 17 passed, 0 failed`. It uses about 30 Postman API
calls. Optional: `set POSTMAN_WORKSPACE_ID=<id>` to pick the workspace (default:
your first personal one), `set POSTMAN_REGION=eu` for EU accounts.

## In Docker

Run in Command Prompt, inside this `postman` folder:

```cmd
docker build -t mcp-postman .
docker run --rm -p 127.0.0.1:8000:8000 mcp-postman
```

The image installs exactly the versions pinned in `requirements.txt`, the same ones the
security scan checks.
The image includes a `HEALTHCHECK` that calls `/health` every 30 seconds, so
`docker ps` shows the container as `healthy` or `unhealthy`.

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

## Compared with the official Postman MCP server

| | Official (`mcp.postman.com`) | This connector |
|---|---|---|
| Auth | OAuth (US) or API key as Bearer | API key per request (`X-Api-Key` or Bearer) |
| Workspaces, collections, environments: list and read | Yes | Yes |
| Create collections and environments | Yes | Yes |
| Delete collections and environments | Yes | Yes, marked destructive |
| Secret environment values | Returned as stored | Always masked |
| Add a request to an existing collection | Yes | Yes (`add_request`) |
| Change, add or remove environment variables | Yes | Yes (`update_environment`, targeted changes only, never a full replace) |
| Edit or delete individual requests, folders, create workspaces | Yes | Not yet |
| Mocks, monitors, specs, comments, tags (the "full" set, 100+ tools) | Yes | No |

## Security checks (2026-10-30)

Run on the whole repo with the company's verification steps:

| Check | Tool | Result |
|---|---|---|
| Secrets in code and full git history | Gitleaks 8.30 | No leaks found |
| Known vulnerabilities in pinned libraries (`uv.lock` and `requirements.txt`) | Trivy 0.76 | 0 (all severities) |
| Dockerfile misconfiguration | Trivy 0.76 | 0 |
| Signed release artifacts | Cosign | None published, so not applicable |
| Library licenses | pip-licenses | All permissive (MIT, BSD, Apache-2.0, ISC; `certifi` is MPL-2.0, used unmodified) |

This folder has no LICENSE file of its own yet; the license for company code is
pending a decision. Re-run the scans after any dependency change.

## Status (2026-10-30)

- 21 automated tests against a fake Postman with two accounts. Deliberately breaking
  each protection makes tests fail: caller isolation (8), secret masking (1), redirects
  followed (1), and for the edit tools: variables removed in the wrong order (1), all
  changes sent in one call (1), values echoed back (1), secret flag ignored (1), missing
  keys not checked (1), wrong field name for request headers (1), whole environment
  replaced instead of patched (2).
- Redirects are refused with a clear error and never followed, so credentials cannot be
  sent to another address. A test redirects to a trap address and proves it is never reached.
- Real Postman account: `scripts/verify_e2e.py` passes 17 of 17, each create, edit and delete
  confirmed directly in Postman: `add_request` left the existing requests untouched, and
  `update_environment` changed, added and removed one variable each while keeping the secret.
- Not yet tested through the workflow designer with an agent calling the tools.
