# Elasticsearch MCP connector (FastMCP, HTTP, multi-tenant)

A stateless FastMCP server over streamable HTTP. Each caller sends **their own
cluster address and credentials on every request**; the server stores nothing.

## Quick start

```bash
uv sync --extra dev
uv run pytest -q        # 34 tests, about 10 seconds, no Elasticsearch needed
```

The tests start the real connector next to two fake Elasticsearch clusters and
check that concurrent callers never see each other's data, and that
unsafe cluster URLs are refused.

## Run it

```bash
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
| `search` | Query DSL search (size up to 100, paging, sort, field filter) | read-only |
| `count` | Count documents matching a query | read-only |
| `esql_query` | ES\|QL query (needs Elasticsearch 8.11+) | read-only |
| `get_document` | Fetch one document by ID | read-only |
| `index_document` | Create or replace a document | **write** |
| `delete_document` | Delete a document | **write, destructive** |

What a caller can actually do is limited by their own API key's privileges,
so give callers a read-only key if they should not write.

## Try it against a real Elasticsearch (Docker)

1. Start a throwaway cluster with security off (testing only):
   ```bash
   docker run --rm -p 9200:9200 -e discovery.type=single-node -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms512m -Xmx512m" docker.elastic.co/elasticsearch/elasticsearch:8.15.0
   ```
2. Start the connector allowing localhost (second terminal):
   - PowerShell: `$env:ES_ALLOWED_HOSTS = "localhost"; uv run python -m connectors.elasticsearch.server`
   - cmd: `set ES_ALLOWED_HOSTS=localhost` then `uv run python -m connectors.elasticsearch.server`
3. Call it as one caller (third terminal), shell syntax as in `scripts/try_it.py`:
   `ES_URL=http://localhost:9200`, `ES_API_KEY=anything`, then `uv run python scripts/try_it.py`.
   With security off, Elasticsearch ignores the key, but the connector still requires the header.

If the connector itself runs in Docker, use `http://host.docker.internal:9200`
as the cluster URL and put `host.docker.internal` in `ES_ALLOWED_HOSTS`.

## In Docker

```bash
docker build -t mcp-elasticsearch .
docker run --rm -p 127.0.0.1:8000:8000 -e ES_ALLOWED_HOSTS=es.example.com mcp-elasticsearch
```

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

## Status (verified 2026-10-28)

- Automated suite against fake clusters: 34 passing. Breaking the allowlist on purpose makes 3 of them fail.
- **Not yet run against a real Elasticsearch.** Do the Docker steps above once.
