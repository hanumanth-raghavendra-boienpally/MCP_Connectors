"""A tiny fake Elasticsearch for tests.

Each fake server is one "cluster" with its own API key and its own data. Every
request is recorded so tests can assert exactly which key reached which
cluster for which call.
"""

import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

CALLS: list[dict] = []  # {"cluster": str, "method": str, "path": str, "auth": str | None, "body": dict | None}


def make_app(cluster: str, api_key: str, docs: list[dict]) -> Starlette:
    store = {d["_id"]: dict(d) for d in docs}

    def check(request: Request, body=None):
        auth = request.headers.get("authorization")
        CALLS.append(
            {"cluster": cluster, "method": request.method, "path": request.url.path, "auth": auth, "body": body}
        )
        if auth != f"ApiKey {api_key}":
            return JSONResponse(
                {"error": {"type": "security_exception", "reason": "unable to authenticate"}, "status": 401},
                status_code=401,
            )
        return None

    async def health(request: Request):
        if (r := check(request)) is not None:
            return r
        return JSONResponse(
            {"cluster_name": cluster, "status": "green", "number_of_nodes": 1, "active_shards": 3, "unassigned_shards": 0}
        )

    async def cat_indices(request: Request):
        if (r := check(request)) is not None:
            return r
        return JSONResponse(
            [{"index": f"{cluster}-logs", "health": "green", "status": "open", "docs.count": str(len(store)), "store.size": "1kb"}]
        )

    async def cat_shards(request: Request):
        if (r := check(request)) is not None:
            return r
        name = f"{cluster}-logs"
        return JSONResponse([
            {"index": name, "shard": "0", "prirep": "p", "state": "STARTED", "docs": str(len(store)),
             "store": "1kb", "node": f"{cluster}-node", "unassigned.reason": None},
            {"index": name, "shard": "1", "prirep": "p", "state": "STARTED", "docs": "0",
             "store": "1kb", "node": f"{cluster}-node", "unassigned.reason": None},
            {"index": name, "shard": "0", "prirep": "r", "state": "UNASSIGNED", "docs": None,
             "store": None, "node": None, "unassigned.reason": "INDEX_CREATED"},
        ])

    async def mapping(request: Request):
        if (r := check(request)) is not None:
            return r
        idx = request.path_params["index"]
        return JSONResponse({idx: {"mappings": {"properties": {"message": {"type": "text"}}}}})

    async def search(request: Request):
        body = await request.json()
        if (r := check(request, body)) is not None:
            return r
        query = body.get("query", {})
        hits = list(store.values())
        if "term" in query:
            (field, value), = query["term"].items()
            hits = [h for h in hits if h["_source"].get(field) == value]
        size = body.get("size", 10)
        return JSONResponse(
            {
                "took": 2,
                "hits": {
                    "total": {"value": len(hits), "relation": "eq"},
                    "hits": [
                        {"_id": h["_id"], "_index": f"{cluster}-logs", "_score": 1.0, "_source": h["_source"]}
                        for h in hits[:size]
                    ],
                },
            }
        )

    async def count(request: Request):
        raw = await request.body()
        if (r := check(request)) is not None:
            return r
        return JSONResponse({"count": len(store)})

    async def esql(request: Request):
        body = await request.json()
        if (r := check(request, body)) is not None:
            return r
        return JSONResponse(
            {"columns": [{"name": "cluster", "type": "keyword"}], "values": [[cluster]] * 5}
        )

    async def get_doc(request: Request):
        if (r := check(request)) is not None:
            return r
        doc = store.get(request.path_params["id"])
        if doc is None:
            return JSONResponse({"_index": f"{cluster}-logs", "_id": request.path_params["id"], "found": False}, status_code=404)
        return JSONResponse({"_index": f"{cluster}-logs", "_id": doc["_id"], "_version": 1, "found": True, "_source": doc["_source"]})

    async def put_doc(request: Request):
        body = await request.json()
        if (r := check(request, body)) is not None:
            return r
        doc_id = request.path_params.get("id", f"auto-{len(store) + 1}")
        existed = doc_id in store
        store[doc_id] = {"_id": doc_id, "_source": body}
        return JSONResponse({"_id": doc_id, "result": "updated" if existed else "created", "_version": 1})

    async def delete_doc(request: Request):
        if (r := check(request)) is not None:
            return r
        doc_id = request.path_params["id"]
        if store.pop(doc_id, None) is None:
            return JSONResponse({"_id": doc_id, "result": "not_found"}, status_code=404)
        return JSONResponse({"_id": doc_id, "result": "deleted"})

    async def redirect(request: Request):
        check(request)
        return RedirectResponse("/trap", status_code=302)

    async def trap(request: Request):
        check(request)
        return JSONResponse({"took": 1, "hits": {"total": {"value": 0}, "hits": []}})

    async def too_many(request: Request):
        check(request)
        return JSONResponse(
            {"error": {"type": "es_rejected_execution_exception", "reason": "busy"}, "status": 429},
            status_code=429,
            headers={"Retry-After": "7"},
        )

    return Starlette(
        routes=[
            Route("/_cluster/health", health),
            Route("/_cat/indices", cat_indices),
            Route("/_cat/indices/{pattern}", cat_indices),
            Route("/_cat/shards", cat_shards),
            Route("/_cat/shards/{pattern}", cat_shards),
            Route("/_query", esql, methods=["POST"]),
            Route("/busy/_search", too_many, methods=["POST"]),
            Route("/redirect-me/_search", redirect, methods=["POST"]),
            Route("/trap", trap, methods=["GET", "POST"]),
            Route("/{index}/_mapping", mapping),
            Route("/{index}/_search", search, methods=["POST"]),
            Route("/{index}/_count", count, methods=["POST"]),
            Route("/{index}/_doc", put_doc, methods=["POST"]),
            Route("/{index}/_doc/{id}", get_doc, methods=["GET"]),
            Route("/{index}/_doc/{id}", put_doc, methods=["PUT"]),
            Route("/{index}/_doc/{id}", delete_doc, methods=["DELETE"]),
        ]
    )


def start(app: Starlette, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server
