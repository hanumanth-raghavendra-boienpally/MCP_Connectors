"""A small, stateful fake of the Postman API for tests.

Each API key is a separate account with its own workspaces, collections and
environments. Every request is recorded so tests can assert exactly which key
reached Postman for which call.
"""

import threading
import time
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

CALLS: list[dict] = []  # {"method", "path", "key", "query", "body"}
MODE = {"drop_writes": False}  # True: writes report success but store nothing


def new_account(username: str, workspace_name: str) -> dict:
    ws = str(uuid.uuid4())
    return {
        "user": {"id": abs(hash(username)) % 10**8, "username": username, "fullName": username.title(),
                 "teamName": "", "teamId": 0},
        "workspaces": {ws: {"id": ws, "name": workspace_name, "type": "personal", "visibility": "personal"}},
        "collections": {},   # id -> {"workspace": ws, "collection": {...}}
        "environments": {},  # id -> {"workspace": ws, "environment": {...}}
    }


ACCOUNTS: dict[str, dict] = {}


def reset() -> None:
    ACCOUNTS.clear()
    ACCOUNTS["PMAK-user-a"] = new_account("alice", "Alice Workspace")
    ACCOUNTS["PMAK-user-b"] = new_account("bob", "Bob Workspace")
    CALLS.clear()
    MODE["drop_writes"] = False


reset()


def _unauth():
    return JSONResponse(
        {"error": {"name": "AuthenticationError",
                   "message": "Invalid API Key. Every request requires a valid API Key to be sent."}},
        status_code=401,
    )


async def _account(request: Request, body=None):
    key = request.headers.get("x-api-key")
    CALLS.append({"method": request.method, "path": request.url.path, "key": key,
                  "query": dict(request.query_params), "body": body})
    return ACCOUNTS.get(key)


def _not_found(kind: str, id_: str):
    return JSONResponse({"error": {"name": "instanceNotFoundError", "message": f"We could not find the {kind} you are looking for"}},
                        status_code=404)


def _default_ws(acct: dict, request: Request) -> str | None:
    ws = request.query_params.get("workspace")
    if ws:
        return ws if ws in acct["workspaces"] else None
    return next(iter(acct["workspaces"]))


async def me(request: Request):
    if request.headers.get("x-api-key") == "PMAK-ratelimited":
        await _account(request)
        return JSONResponse({"type": "about:blank", "title": "rateLimited", "detail": "Too many requests", "status": 429},
                            status_code=429, headers={"RetryAfter": "7"})
    acct = await _account(request)
    if acct is None:
        return _unauth()
    return JSONResponse({"user": acct["user"], "operations": [{"name": "api_usage", "limit": 1000, "usage": 10}]})


async def workspaces(request: Request):
    acct = await _account(request)
    if acct is None:
        return _unauth()
    return JSONResponse({"workspaces": list(acct["workspaces"].values())})


async def workspace(request: Request):
    acct = await _account(request)
    if acct is None:
        return _unauth()
    ws = request.path_params["id"]
    if ws == "redirect":
        return RedirectResponse("/trap", status_code=302)
    if ws not in acct["workspaces"]:
        return _not_found("workspace", ws)
    cols = [{"id": i, "name": c["collection"]["info"]["name"], "uid": f"1-{i}"}
            for i, c in acct["collections"].items() if c["workspace"] == ws]
    envs = [{"id": i, "name": e["environment"]["name"], "uid": f"1-{i}"}
            for i, e in acct["environments"].items() if e["workspace"] == ws]
    return JSONResponse({"workspace": {**acct["workspaces"][ws], "collections": cols, "environments": envs}})


async def collection_requests(request: Request):
    body = await request.json()
    acct = await _account(request, body)
    if acct is None:
        return _unauth()
    cid = request.path_params["id"]
    if cid not in acct["collections"]:
        return _not_found("collection", cid)
    rid = str(uuid.uuid4())
    req = {"method": body.get("method", "GET"), "url": {"raw": body.get("url")}}
    if body.get("headerData"):
        req["header"] = [{"key": h["key"], "value": h["value"]} for h in body["headerData"]]
    if body.get("dataMode") == "raw":
        req["body"] = {"mode": "raw", "raw": body.get("rawModeData")}
    item = {"id": rid, "name": body.get("name"), "request": req}
    if not MODE["drop_writes"]:
        items = acct["collections"][cid]["collection"]["item"]
        folder = request.query_params.get("folder")
        if folder:
            target = next((it for it in items if it.get("id") == folder and "item" in it), None)
            if target is None:
                return _not_found("folder", folder)
            target["item"].append(item)
        else:
            items.append(item)
    return JSONResponse({"data": {"id": rid, "name": body.get("name"), "collection": cid}, "model_id": rid,
                         "meta": {"action": "create", "model": "request"}})


async def trap(request: Request):
    await _account(request)
    return JSONResponse({"workspace": {"id": "trap", "name": "trap"}})


async def collections(request: Request):
    body = await request.json() if request.method == "POST" else None
    acct = await _account(request, body)
    if acct is None:
        return _unauth()
    ws = _default_ws(acct, request)
    if ws is None:
        return _not_found("workspace", request.query_params.get("workspace", ""))
    if request.method == "GET":
        return JSONResponse({"collections": [
            {"id": i, "uid": f"1-{i}", "name": c["collection"]["info"]["name"], "updatedAt": "2026-01-01T00:00:00Z"}
            for i, c in acct["collections"].items() if c["workspace"] == ws]})
    col = body.get("collection") or {}
    if not col.get("info", {}).get("name") or "item" not in col:
        return JSONResponse({"error": {"name": "malformedRequestError", "message": "collection info/item missing"}},
                            status_code=400)
    cid = str(uuid.uuid4())
    if not MODE["drop_writes"]:
        stored = {"info": {**col["info"], "_postman_id": cid},
                  "item": [{**it, "id": str(uuid.uuid4())} for it in col["item"]]}
        acct["collections"][cid] = {"workspace": ws, "collection": stored}
    return JSONResponse({"collection": {"id": cid, "name": col["info"]["name"], "uid": f"1-{cid}"}})


async def collection(request: Request):
    acct = await _account(request)
    if acct is None:
        return _unauth()
    cid = request.path_params["id"]
    if cid not in acct["collections"]:
        return _not_found("collection", cid)
    if request.method == "DELETE":
        if not MODE["drop_writes"]:
            acct["collections"].pop(cid)
        return JSONResponse({"collection": {"id": cid, "uid": f"1-{cid}"}})
    return JSONResponse({"collection": acct["collections"][cid]["collection"]})


async def environments(request: Request):
    body = await request.json() if request.method == "POST" else None
    acct = await _account(request, body)
    if acct is None:
        return _unauth()
    ws = _default_ws(acct, request)
    if ws is None:
        return _not_found("workspace", request.query_params.get("workspace", ""))
    if request.method == "GET":
        return JSONResponse({"environments": [
            {"id": i, "uid": f"1-{i}", "name": e["environment"]["name"], "updatedAt": "2026-01-01T00:00:00Z"}
            for i, e in acct["environments"].items() if e["workspace"] == ws]})
    env = body.get("environment") or {}
    eid = str(uuid.uuid4())
    if not MODE["drop_writes"]:
        acct["environments"][eid] = {"workspace": ws, "environment": {"id": eid, "name": env.get("name"),
                                                                      "values": env.get("values", [])}}
    return JSONResponse({"environment": {"id": eid, "name": env.get("name"), "uid": f"1-{eid}"}})


async def environment(request: Request):
    acct = await _account(request)
    if acct is None:
        return _unauth()
    eid = request.path_params["id"]
    if eid not in acct["environments"]:
        return _not_found("environment", eid)
    if request.method == "DELETE":
        if not MODE["drop_writes"]:
            acct["environments"].pop(eid)
        return JSONResponse({"environment": {"id": eid, "uid": f"1-{eid}"}})
    if request.method == "PATCH":
        ops = await request.json()
        CALLS[-1]["body"] = ops
        if len({o.get("op") for o in ops}) != 1:
            return JSONResponse({"error": {"name": "invalidParamsError", "message": "One type of operation at a time"}},
                                status_code=400)
        values = [dict(v) for v in acct["environments"][eid]["environment"]["values"]]
        try:
            for o in ops:
                parts = o["path"].strip("/").split("/")
                if parts[0] != "values":
                    raise ValueError(o["path"])
                i = int(parts[1])
                if o["op"] == "replace" and len(parts) == 3:
                    values[i][parts[2]] = o["value"]
                elif o["op"] == "add" and len(parts) == 2:
                    if i > len(values):
                        raise IndexError(i)
                    values.insert(i, dict(o["value"]))
                elif o["op"] == "remove" and len(parts) == 2:
                    values.pop(i)
                else:
                    raise ValueError(o)
        except (ValueError, IndexError, KeyError) as exc:
            return JSONResponse({"error": {"name": "invalidParamsError", "message": f"bad op: {exc}"}}, status_code=400)
        if not MODE["drop_writes"]:
            acct["environments"][eid]["environment"]["values"] = values
        return JSONResponse({"environment": acct["environments"][eid]["environment"]})
    return JSONResponse({"environment": acct["environments"][eid]["environment"]})


app = Starlette(routes=[
    Route("/me", me),
    Route("/workspaces", workspaces),
    Route("/workspaces/{id}", workspace),
    Route("/trap", trap, methods=["GET", "POST"]),
    Route("/collections", collections, methods=["GET", "POST"]),
    Route("/collections/{id}", collection, methods=["GET", "DELETE"]),
    Route("/collections/{id}/requests", collection_requests, methods=["POST"]),
    Route("/environments", environments, methods=["GET", "POST"]),
    Route("/environments/{id}", environment, methods=["GET", "DELETE", "PATCH"]),
])


def start(port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server
