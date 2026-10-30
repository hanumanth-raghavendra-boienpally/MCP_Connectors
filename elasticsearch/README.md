# Elasticsearch MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
cluster address and credentials on every request**; the server stores nothing.

## Quick start

Needs **Python 3.12 or newer** and [`uv`](https://docs.astral.sh/uv/).

```cmd
uv sync --extra dev
uv run pytest -q        # 37 tests, about 10 seconds, no Elasticsearch needed
```

The tests start the real connector next to two fake Elasticsearch clusters and
check that concurrent callers never see each other's data, and that
unsafe cluster URLs are refused.

Without `uv` (classic pip), in PowerShell inside this `elasticsearch` folder:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python -m pytest -q
```

## Run it

```cmd
uv run python -m connectors.elasticsearch.server   # serves http://0.0.0.0:8000/mcp
```

`HOST` and `PORT` change where it listens. Health check: `GET /health` returns `ok`.

### Operator setting: `ES_ALLOWED_HOSTS`

Callers choose which cluster to talk to, so the server must limit that or it
can be pointed at internal services (SSRF).

| `ES_ALLOWED_HOSTS` | Behaviour |
|---|---|
| set, e.g. `es.example.com,*.es.cloud.io` | Only these hosts are accepted (http or https). **Use this in production.** `*.x.io` matches `a.x.io`, not `x.io`. |
| unset | Any host over **https** whose addresses are all public. Private, loopback and link-local addresses are refused. DNS rebinding is not fully covered, so prefer the allowlist. |

## What callers send

```json
{
  "transport": "streamable_http",
  "url": "https://<your-host>/mcp",
  "headers": {
    "X-Elasticsearch-URL": "https://<cluster-host>:9243",
    "Authorization": "ApiKey <base64 id:key>"
  }
}
```

`Authorization` may also be `Basic <base64 user:pass>` or `Bearer <token>`.
Never put credentials inside the URL.

## Tools

| Tool | What it does | Kind |
|---|---|---|
| `cluster_health` | Status, nodes, shards | read-only |
| `list_indices` | Indices with health, doc count, size | read-only |
| `get_mapping` | Field names and types of an index | read-only |
| `get_shards` | Shards per index, their state and node, and why any are unassigned (why an index is yellow or red) | read-only |
| `search` | Query DSL search (size up to 100, paging, sort, field filter) | read-only |
| `count` | Count documents matching a query | read-only |
| `esql_query` | ES\|QL query (needs Elasticsearch 8.11+) | read-only |
| `get_document` | Fetch one document by ID | read-only |
| `index_document` | Create or replace a document | **write** |
| `delete_document` | Delete a document | **write, destructive** |

What a caller can actually do is limited by their own API key's privileges,
so give callers a read-only key if they should not write.

## Try it against a real Elasticsearch (Docker)

Three terminals (Windows Command Prompt syntax; on bash use `export` instead of `set`).

**Terminal 1** (any folder): start a throwaway Elasticsearch with security off. Testing only.
```cmd
docker run --rm -p 127.0.0.1:9201:9200 -e discovery.type=single-node -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" docker.elastic.co/elasticsearch/elasticsearch:8.15.0
```
Wait about 60 seconds, then open `http://127.0.0.1:9201` in a browser. You should see JSON containing `"cluster_name"`.

**Terminal 2** (in this `elasticsearch` folder): start the connector, allowing that host.
```cmd
set ES_ALLOWED_HOSTS=127.0.0.1
uv run python -m connectors.elasticsearch.server
```

**Terminal 3** (in this `elasticsearch` folder): call it as one caller.
```cmd
set ES_URL=http://127.0.0.1:9201
set ES_API_KEY=anything
uv run python scripts/try_it.py
```
With security off, Elasticsearch ignores the key, but the connector still requires the header.

**Why `127.0.0.1` and port 9201?** On some Windows machines `localhost:9200` stops answering from the host even though Elasticsearch is healthy inside the container (check with `docker exec <container-id> curl -s http://localhost:9200`). Using `127.0.0.1` and a fresh host port avoids it. If it still hangs, quit Docker Desktop, run `wsl --shutdown`, and start Docker Desktop again.

If the connector itself runs in Docker, use `http://host.docker.internal:9201` as the cluster URL and put `host.docker.internal` in `ES_ALLOWED_HOSTS`.

## End-to-end check: did it really happen in Elasticsearch?

`scripts/verify_e2e.py` does each action **through the connector**, then asks
Elasticsearch **directly** whether it happened (write, read back, search, delete),
pauses so you can open the document in a browser and screenshot it, then deletes
its own throwaway index (`mcp-verify-<random>`). Run it with terminals 1 and 2
above still open, from this folder:
```cmd
set ES_URL=http://127.0.0.1:9201
set ES_API_KEY=anything
uv run python scripts/verify_e2e.py
```
Expected last line: `Summary: 12 passed, 0 failed`. If the connector or Elasticsearch is
not answering, it says which one instead of failing with a trace.

## In Docker

```cmd
docker build -t mcp-elasticsearch .
docker run --rm -p 127.0.0.1:8000:8000 -e ES_ALLOWED_HOSTS=es.example.com mcp-elasticsearch
```

The image installs exactly the versions pinned in `requirements.txt`, the same ones the
security scan checks.
The image includes a `HEALTHCHECK` that calls `/health` every 30 seconds, so
`docker ps` shows the container as `healthy` or `unhealthy`.

## Rules this connector follows

1. Credentials and the cluster URL come from the request headers, never from env vars or files.
2. Headers are read with `connector_core.auth` (`include_all=True` is needed to see `Authorization`).
3. A new HTTP client is built per call. No global clients, no caches shared across callers.
4. Stateless: `mcp.run(transport="http", stateless_http=True)`.
5. No header values in logs or error messages.
6. `follow_redirects=False`, so a caller's key is never forwarded to a redirect target.
7. The cluster URL is validated (`connector_core/url_guard.py`) and index names are checked before they reach a URL path.

## This folder is standalone

It shares no code with other connectors. To move it into a repo, copy the
whole `elasticsearch/` folder and leave out `.venv`, `__pycache__`,
`.pytest_cache` and `*.egg-info`.

## Compared with the official Elasticsearch MCP server

Elastic's own server (`elastic/mcp-server-elasticsearch`) is **deprecated** and only gets
critical security fixes; its successor is the Agent Builder MCP endpoint inside Kibana 9.2+.

| | Official (deprecated) | This connector |
|---|---|---|
| Credentials | Fixed per server in environment variables (one user) | Per request in headers (any number of users) |
| Cluster address | Fixed per server | Per request, checked against an allowlist (SSRF protection) |
| `list_indices`, `get_mappings`, `search`, ES\|QL | Yes | Yes |
| `get_shards` | Yes | Yes |
| `cluster_health`, `count`, `get_document` | No | Yes |
| Write tools (`index_document`, `delete_document`) | No | Yes, marked destructive |
| Needs Kibana | No (successor: yes, 9.2+) | No; any Elasticsearch 8.11+ (ES\|QL), 8.x for the rest |

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

- 37 automated tests against two fake clusters. Deliberately breaking each protection
  makes tests fail: first caller's key reused (7), first caller's cluster reused (6),
  private addresses allowed (6), wildcard look-alike hosts (1), index-name check off (1),
  key leaked in an error (1), redirects followed (1), and for `get_shards`: primary and
  replica swapped (1), pattern not validated (1), state counts wrong (1).
- Redirects are refused with a clear error and never followed, so credentials cannot be
  sent to another address. A test redirects to a trap address and proves it is never reached.
- Real Elasticsearch 8.15 in Docker: `scripts/verify_e2e.py` passes 12 of 12, each action
  confirmed directly in Elasticsearch, including `get_shards` matching Elasticsearch's own
  shard list.
- Not yet tested through the workflow designer with an agent calling the tools.
