# Slack MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
Slack token on every request**; the server stores nothing.

This folder is standalone: it shares no code with other connectors in the repo.

## Quick start

Run in Command Prompt, inside this `slack` folder:

```cmd
uv sync --extra dev
uv run pytest -q
```
Expect `9 passed` in about 10 seconds. No Slack account is needed: the tests
start the real connector next to a fake Slack and check that concurrent callers
never see each other's data.

## Run the connector

Run in Command Prompt, inside this `slack` folder:

```cmd
uv run python -m connectors.slack.server
```
It serves `http://0.0.0.0:8000/mcp`. Set `HOST=127.0.0.1` to keep it reachable
only from your own machine and `PORT` to change the port. Health check:
`GET /health` returns `ok`.

## What callers send

```json
{
  "transport": "streamable_http",
  "url": "https://<your-host>/mcp",
  "headers": { "Authorization": "Bearer <slack-bot-token>" }
}
```

## Tools

| Tool | What it does | Kind |
|---|---|---|
| `list_channels` | Channels the token can see | read-only |
| `get_channel_history` | Recent messages in a channel | read-only |
| `get_thread_replies` | Replies in a thread | read-only |
| `list_users` | Workspace members | read-only |
| `post_message` | Post a message, optionally as a thread reply | **write** |
| `add_reaction` | Add an emoji reaction | **write** |

Bot scopes needed: `channels:read`, `channels:history`, `chat:write`,
`reactions:write`, `users:read`. Add `reactions:read` for the end-to-end check
below, and `groups:read` / `groups:history` for private channels.

## Try it as one caller

Environment variables last only for that terminal window, and the syntax
differs by shell:

| Shell | Set the token |
|---|---|
| PowerShell (prompt starts with `PS`) | `$env:SLACK_TOKEN = "xoxb-..."` |
| Command Prompt (cmd) | `set SLACK_TOKEN=xoxb-...` (no quotes) |
| bash / zsh | `export SLACK_TOKEN=xoxb-...` |

With the connector running in one window, run this in a second window, inside
this `slack` folder:

```cmd
uv run python scripts/try_it.py
```
It lists the tools and the channels (with their IDs) for that token.

## End-to-end check: did it really happen in Slack?

`scripts/verify_e2e.py` does each action **through the connector**, then asks
Slack **directly** whether it happened: it posts a test message, confirms it
exists, reads it back, reacts, replies in a thread, then pauses so you can look
at the channel in the Slack app (and screenshot it), and finally deletes its own
test messages.

Use a throwaway channel in a personal workspace and invite the bot to it
(`/invite @<bot-name>`). Then, with the connector running in one window, run this
in a second window, inside this `slack` folder (use your own token and channel ID;
`try_it.py` prints the IDs):

```cmd
set SLACK_TOKEN=xoxb-...
set SLACK_CHANNEL_ID=C0123456789
uv run python scripts/verify_e2e.py
```
Without the `reactions:read` scope, the reaction's direct check is skipped and
you confirm it by eye.

## In Docker

Run in Command Prompt, inside this `slack` folder:

```cmd
docker build --build-arg CONNECTOR=slack -t mcp-slack .
docker run --rm -p 127.0.0.1:8000:8000 mcp-slack
```

## Rules this connector follows

1. **Credentials come from the request**, never from env vars or config files.
2. **Headers are read with `connector_core.auth`**. FastMCP strips
   `Authorization` unless `include_all=True`, and the helper handles that.
3. **The API client is built per call** (`connector_core.http.make_client`). No
   global clients, no caches shared across callers.
4. **Stateless**: `mcp.run(transport="http", stateless_http=True)`.
5. **No header values in logs or error messages.**
6. **No redirects followed**, so a caller's token is never forwarded elsewhere.
7. Read-only tools carry `readOnlyHint`; write tools are marked as writes.

## Deploying

- The server is stateless, so it can run as several instances behind a load
  balancer. The host's `PORT` variable is respected.
- Decide who can reach the URL (public, gateway or VPN). Anyone who can reach it
  can use it with their own credentials.
- Do not bake credentials into the image or environment. `SLACK_API_BASE` exists
  only so tests can point the connector at a fake Slack.

## Moving this into a repo

Copy the whole `slack/` folder and leave out `.venv`, `__pycache__`,
`.pytest_cache` and `*.egg-info`.

## Status

- 9 automated tests against a fake Slack.
- Tools exercised against a real Slack workspace (personal test workspace).
- `scripts/verify_e2e.py` reproduces the real-workspace check and prints a summary.
- Not yet tested through the workflow designer with an agent calling the tools.