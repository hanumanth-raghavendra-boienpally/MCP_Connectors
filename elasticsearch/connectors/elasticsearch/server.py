"""Elasticsearch MCP connector: streamable HTTP, stateless, multi-tenant.

Every caller sends their own cluster and credentials on every request:

  X-Elasticsearch-URL: https://my-cluster.example.com:9243
  Authorization: ApiKey <base64 id:key>     (or 'Basic ...' or 'Bearer ...')

The server holds no credentials and no per-user state.
"""

import os
import re
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from connector_core.auth import get_authorization, get_header
from connector_core.http import make_client
from connector_core.url_guard import validate_base_url

mcp = FastMCP(
    "elasticsearch",
    instructions=(
        "Elasticsearch access. On every request send your cluster address as "
        "'X-Elasticsearch-URL: https://host:port' and your credentials as "
        "'Authorization: ApiKey <key>' (or Basic / Bearer)."
    ),
)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}

MAX_LISTED_INDICES = 200
_INDEX_RE = re.compile(r"^[A-Za-z0-9_.*,+\-]{1,255}$")


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _index(value: str) -> str:
    """Check an index name, comma list or wildcard pattern and make it URL-safe."""
    v = value.strip()
    parts = v.split(",")
    if (
        not _INDEX_RE.fullmatch(v)
        or any(p in {"", ".", ".."} for p in parts)
        or any(p.startswith("_") and p != "_all" for p in parts)
    ):
        raise ToolError("Invalid index name or pattern.")
    return quote(v, safe=",*")


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if status < 400:
        return
    if status == 401:
        raise ToolError("Elasticsearch rejected the credentials (HTTP 401).")
    if status == 429:
        retry = resp.headers.get("retry-after")
        raise ToolError(
            "Elasticsearch is rate limiting this request (HTTP 429)."
            + (f" Retry after {retry} seconds." if retry else "")
        )
    detail = ""
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            detail = f"{err.get('type', 'error')}: {err.get('reason', '')}"
        elif isinstance(err, str):
            detail = err
    except (ValueError, AttributeError):
        pass
    if not detail and status == 404:
        detail = "not found"
    detail = f" ({detail[:300]})" if detail else ""
    raise ToolError(f"Elasticsearch returned HTTP {status}{detail}.")


async def _es(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call Elasticsearch as the current caller, on the caller's own cluster."""
    base = await validate_base_url(get_header("x-elasticsearch-url"))
    auth = get_authorization()
    headers = {"Authorization": auth, "Accept": "application/json"}
    async with make_client(base, headers) as client:
        try:
            resp = await client.request(method, path, params=params, json=body)
        except httpx.TimeoutException:
            raise ToolError("Elasticsearch timed out.") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not reach Elasticsearch ({type(exc).__name__}).") from None
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError:
        raise ToolError("Elasticsearch returned a response that is not JSON.") from None


def _total(hits: dict[str, Any]) -> int | None:
    total = hits.get("total")
    return total.get("value") if isinstance(total, dict) else total


@mcp.tool(annotations=READ_ONLY)
async def cluster_health() -> dict[str, Any]:
    """Show the cluster's health: status (green/yellow/red), nodes and shards."""
    d = await _es("GET", "/_cluster/health")
    keys = [
        "cluster_name", "status", "number_of_nodes", "number_of_data_nodes",
        "active_primary_shards", "active_shards", "relocating_shards",
        "initializing_shards", "unassigned_shards",
    ]
    return {k: d.get(k) for k in keys}


@mcp.tool(annotations=READ_ONLY)
async def list_indices(
    pattern: Annotated[str, Field(description="Index name or wildcard, e.g. 'logs-*'")] = "*",
) -> dict[str, Any]:
    """List indices with health, status, document count and size."""
    rows = await _es(
        "GET",
        f"/_cat/indices/{_index(pattern)}",
        params={"format": "json", "h": "index,health,status,docs.count,store.size", "s": "index"},
    )
    rows = rows if isinstance(rows, list) else []
    return {
        "indices": [
            {
                "index": r.get("index"),
                "health": r.get("health"),
                "status": r.get("status"),
                "docs_count": r.get("docs.count"),
                "store_size": r.get("store.size"),
            }
            for r in rows[:MAX_LISTED_INDICES]
        ],
        "truncated": len(rows) > MAX_LISTED_INDICES,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_mapping(
    index: Annotated[str, Field(description="Index name or pattern")],
) -> dict[str, Any]:
    """Get the field mappings (names and types) of an index."""
    return await _es("GET", f"/{_index(index)}/_mapping")


@mcp.tool(annotations=READ_ONLY)
async def search(
    index: Annotated[str, Field(description="Index name, comma list or pattern, e.g. 'logs-*'")],
    query: Annotated[
        dict[str, Any] | None,
        Field(description='Query DSL object, e.g. {"match": {"message": "error"}}. Omit to match everything.'),
    ] = None,
    size: Annotated[int, Field(ge=1, le=100, description="Number of hits to return")] = 10,
    offset: Annotated[int, Field(ge=0, le=10000, description="Hits to skip (paging)")] = 0,
    sort: Annotated[list[Any] | None, Field(description='e.g. [{"@timestamp": "desc"}]')] = None,
    fields: Annotated[list[str] | None, Field(description="Only return these _source fields")] = None,
) -> dict[str, Any]:
    """Search an index with the Elasticsearch Query DSL."""
    body: dict[str, Any] = {"query": query or {"match_all": {}}, "size": size, "from": offset}
    if sort:
        body["sort"] = sort
    if fields:
        body["_source"] = fields
    d = await _es("POST", f"/{_index(index)}/_search", params={"track_total_hits": "true"}, body=body)
    hits = d.get("hits", {})
    return {
        "total": _total(hits),
        "took_ms": d.get("took"),
        "hits": [
            {"id": h.get("_id"), "index": h.get("_index"), "score": h.get("_score"), "source": h.get("_source")}
            for h in hits.get("hits", [])
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def count(
    index: Annotated[str, Field(description="Index name or pattern")],
    query: Annotated[dict[str, Any] | None, Field(description="Query DSL object; omit to count all")] = None,
) -> dict[str, Any]:
    """Count the documents matching a query."""
    d = await _es("POST", f"/{_index(index)}/_count", body={"query": query} if query else None)
    return {"count": d.get("count")}


@mcp.tool(annotations=READ_ONLY)
async def esql_query(
    query: Annotated[str, Field(min_length=1, description="ES|QL, e.g. 'FROM logs-* | STATS c = COUNT(*) BY level'")],
    max_rows: Annotated[int, Field(ge=1, le=1000, description="Rows to return")] = 100,
) -> dict[str, Any]:
    """Run an ES|QL query (Elasticsearch 8.11 or newer). Read-only."""
    d = await _es("POST", "/_query", params={"format": "json"}, body={"query": query})
    values = d.get("values", [])
    return {
        "columns": [{"name": c.get("name"), "type": c.get("type")} for c in d.get("columns", [])],
        "rows": values[:max_rows],
        "truncated": len(values) > max_rows,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_document(
    index: Annotated[str, Field(description="Index name")],
    doc_id: Annotated[str, Field(min_length=1, max_length=512, description="Document ID")],
) -> dict[str, Any]:
    """Fetch one document by ID."""
    d = await _es("GET", f"/{_index(index)}/_doc/{quote(doc_id, safe='')}")
    return {"id": d.get("_id"), "index": d.get("_index"), "version": d.get("_version"), "source": d.get("_source")}


@mcp.tool(annotations=WRITE)
async def index_document(
    index: Annotated[str, Field(description="Index name (created if it does not exist)")],
    document: Annotated[dict[str, Any], Field(description="The JSON document to store")],
    doc_id: Annotated[str | None, Field(max_length=512, description="Optional ID. An existing document with this ID is replaced.")] = None,
) -> dict[str, Any]:
    """Store a document. With doc_id it creates or replaces; without, Elasticsearch picks an ID."""
    idx = _index(index)
    if doc_id:
        d = await _es("PUT", f"/{idx}/_doc/{quote(doc_id, safe='')}", body=document)
    else:
        d = await _es("POST", f"/{idx}/_doc", body=document)
    return {"id": d.get("_id"), "result": d.get("result"), "version": d.get("_version")}


@mcp.tool(annotations=WRITE)
async def delete_document(
    index: Annotated[str, Field(description="Index name")],
    doc_id: Annotated[str, Field(min_length=1, max_length=512, description="Document ID")],
) -> dict[str, Any]:
    """Delete one document by ID. This cannot be undone."""
    d = await _es("DELETE", f"/{_index(index)}/_doc/{quote(doc_id, safe='')}")
    return {"id": d.get("_id"), "result": d.get("result")}


def main() -> None:
    mcp.run(
        transport="http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
