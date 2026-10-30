# Supabase MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
Supabase personal access token on every request**; the server stores nothing.
It talks only to the Supabase Management API (`https://api.supabase.com`);
callers never supply a URL.

This folder is standalone: it shares no code with other connectors in the repo.

## Quick start

Run in Command Prompt, inside this `supabase` folder:

```cmd
uv sync --extra dev
uv run pytest -q
```
Expect `32 passed` in about 10 seconds. No Supabase account is needed: the tests
start the real connector next to a fake Supabase API with two accounts, each
with a project backed by a real in-memory database.

Without `uv` (classic pip), in PowerShell inside this folder:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python -m pytest -q
```

## Run the connector

Run in Command Prompt, inside this `supabase` folder:

```cmd
uv run python -m connectors.supabase.server
```
It serves `http://0.0.0.0:8000/mcp`. Set `HOST=127.0.0.1` to keep it reachable
only from your own machine and `PORT` to change the port. Health check:
`GET /health` returns `ok`.

## What callers send

```json
{
  "transport": "streamable_http",
  "url": "https://<your-host>/mcp",
  "headers": {
    "Authorization": "Bearer <supabase-personal-access-token>",
    "X-Supabase-Project-Ref": "<project-ref>",
    "X-Supabase-Read-Only": "true"
  }
}
```

| Header | Required | Effect |
|---|---|---|
| `Authorization: Bearer <token>` | yes | The caller's own Supabase personal access token |
| `X-Supabase-Project-Ref` | no | Locks the connection to one project. `project_id` becomes optional, other projects are refused, account-wide tools are disabled |
| `X-Supabase-Read-Only: true` | no | SQL runs as Supabase's read-only database user, write statements are refused before they reach Supabase, migrations are refused |

For agents working on production data, set **both** optional headers.

## Tools

| Group | Tool | What it does | Kind |
|---|---|---|---|
| Account | `list_organizations`, `get_organization` | Organizations and their plan | read |
| Account | `list_projects`, `get_project` | Projects, status, region, Postgres version | read |
| Database | `list_tables` | Tables, estimated rows, RLS on/off; `verbose` adds columns and primary keys | read |
| Database | `list_extensions` | Installed and available Postgres extensions | read |
| Database | `list_migrations` | Applied migrations | read |
| Database | `apply_migration` | Apply DDL as a tracked migration | **write** |
| Database | `execute_sql` | Run SQL; results capped and fenced as untrusted data | **write** (read-only on read-only connections) |
| Debugging | `query_logs` | Read-only query over the project's logs (up to 24 hours back) | read |
| Debugging | `get_advisors` | Security or performance advice (e.g. tables without RLS) | read |
| Development | `get_project_url` | The project's API URL | read |
| Development | `get_publishable_keys` | Client-safe keys only (publishable, legacy anon) | read |
| Development | `generate_typescript_types` | TypeScript types from the live schema | read |
| Edge Functions | `list_edge_functions`, `get_edge_function` | Functions, status, version | read |
| Storage | `list_storage_buckets`, `get_storage_config` | Buckets and Storage settings | read |

## Safety features

1. **Untrusted-data fence.** SQL and log results are wrapped in random
   `<untrusted-data-…>` markers with a warning, so an agent is told not to follow
   instructions hidden in table contents (prompt injection). Like the official
   server's version, this reduces the risk but is not foolproof.
2. **Destructive-SQL guard.** `DROP`, `TRUNCATE`, `ALTER TABLE … DROP`, and
   `DELETE`/`UPDATE` without `WHERE` are refused unless the caller passes
   `allow_destructive=true`. Comments are stripped first, so `/* x */ DROP` is caught.
   It is a best-effort check, not a SQL parser.
3. **Read-only connections, two layers.** The connector refuses write statements
   itself, and Supabase runs the remaining queries as a read-only database user.
4. **Project scoping** enforced on the server, before any request reaches Supabase.
5. **No secret keys, ever.** `get_publishable_keys` asks Supabase with
   `reveal=false` and drops anything that is not publishable or legacy anon.
6. **Capped results** (`max_rows`, default 200, max 1000) so a large table cannot
   flood the agent.
7. Input checks: project refs, schema names, function slugs and migration names are
   validated before they go into URLs or SQL.

## Compared with the official Supabase MCP server

| | Official (`mcp.supabase.com`) | This connector |
|---|---|---|
| Auth | Interactive OAuth login per user | Token in a header per request, so it works in a designer that registers connectors with fixed headers |
| Read-only mode, project scoping | URL options | Per-connection headers, enforced on the server |
| Untrusted-data fence on SQL results | Yes | Yes (also on log results) |
| Destructive-SQL protection | Confirmation form, if the client supports forms | Refused unless `allow_destructive=true`, works with any client |
| Result size cap | No documented cap | `max_rows` (≤1000) |
| Database, debugging, development, Storage read tools | Yes | Yes |
| Edge Function source code, deploy, secrets | Yes | Metadata only; no deploy |
| Create, pause or restore projects; cost tools; branching | Yes | Not included (deliberately: these cost money or are paid-plan only) |
| Docs search | Yes | Not included |
| Update Storage config | Yes | Not included |

## Get a token and a test project

1. Sign in at `https://supabase.com/dashboard` (a free account is fine) and create a
   **new, empty project** for testing. Never test against production.
2. Wait until the project shows as healthy (a minute or two).
3. Open `https://supabase.com/dashboard/account/tokens` and click **Generate new
   token**. Choose resource access **Project** and pick only the test project, then
   grant just these permissions (everything else stays **None**):
   Project Settings, Advisors, Logs, API Keys, Edge Functions, Storage, Storage Config,
   Projects (account-wide), Organizations: **Read**; Database, Migrations: **Read & write**.
   Leave API Key Secrets, Data API JWT Secret, Auth Signing Keys and Edge Function
   Secrets at **None**. Copy the token; it starts with `sbp_` and is shown once.

Never commit the token, paste it into chats, or put it in this folder.

## Try it as one caller

With the connector running in one Command Prompt window, run this in a second
window, inside this `supabase` folder:

```cmd
set SUPABASE_ACCESS_TOKEN=sbp_...
uv run python scripts/try_it.py
```
It prints the tool list and your projects with their refs.

## End-to-end check: did it really happen in Supabase?

`scripts/verify_e2e.py` does each action **through the connector**, then asks the
Supabase API **directly** whether it happened. On your real project it:

1. creates `mcp_verify_<random>` with a tracked migration, inserts two rows, reads
   them back, and confirms each step directly;
2. proves the safety features: a read-only connection's write is refused and the
   row count is unchanged; `DROP` is refused without `allow_destructive`; a scoped
   connection blocks account tools; only client-safe keys come back;
3. pauses so you can see the table and migration in the Supabase dashboard;
4. drops the table, confirms it is gone, and removes its migration record.

With the connector running in one window, run this in a second window, inside
this `supabase` folder:

```cmd
set SUPABASE_ACCESS_TOKEN=sbp_...
uv run python scripts/verify_e2e.py
```
Expected last line: `Summary: 18 passed, 0 failed` (the security-advisor line may be a
warning if the advisor has not caught up yet; that is not a failure). If you have
more than one project, also `set SUPABASE_PROJECT_REF=<ref of the test project>`.

## In Docker

Run in Command Prompt, inside this `supabase` folder:

```cmd
docker build -t mcp-supabase .
docker run --rm -p 127.0.0.1:8000:8000 mcp-supabase
```

## Rules this connector follows

1. The token comes from the request headers, never from env vars or files.
2. Headers are read with `connector_core.auth` (`include_all=True` is needed to see `Authorization`).
3. A new HTTP client is built per call. No global clients, no caches shared across callers.
4. Stateless: `mcp.run(transport="http", stateless_http=True)`.
5. No header values in logs or error messages.
6. `follow_redirects=False`, so a caller's token is never forwarded to a redirect target.
7. The API host is fixed. `SUPABASE_API_BASE` exists only so tests can use a fake Supabase.

## Moving this into a repo

Copy the whole `supabase/` folder and leave out `.venv`, `__pycache__`,
`.pytest_cache` and `*.egg-info`.

## Status (2026-10-30)

- 32 automated tests against a fake Supabase with two accounts. Deliberately
  breaking each safety feature makes tests fail: caller isolation (7), destructive
  guard (11), untrusted fence (3), read-only (2), project scope (2), key filter (1).
- `scripts/verify_e2e.py` against a real Supabase project (fine-grained, project-scoped
  token): 18 of 18 checks pass, each write confirmed directly in Supabase, and the
  read-only, destructive-SQL, project-scope and key-filter protections proven on real data.
- The first real run caught a bug the fake could not: `list_tables` read primary keys
  from `information_schema`, which hides them from read-only users. It now reads
  `pg_catalog` (checked on a real Postgres 16 with a SELECT-only user), and a test
  stops the old approach from coming back.
- Not yet tested through the workflow designer with an agent calling the tools.