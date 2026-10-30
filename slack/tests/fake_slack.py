"""A tiny fake Slack Web API for tests.

Two fake workspaces, chosen by the Bearer token. Every request is recorded so
tests can assert exactly which token reached Slack for which call.
"""

import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

WORKSPACES = {
    "xoxb-user-a": {
        "channels": [{"id": "CA1", "name": "a-general"}, {"id": "CA2", "name": "a-random"}],
        "messages": [{"ts": "1.0", "user": "UA1", "text": "hello from workspace A"}],
    },
    "xoxb-user-b": {
        "channels": [{"id": "CB1", "name": "b-general"}],
        "messages": [{"ts": "2.0", "user": "UB1", "text": "hello from workspace B"}],
    },
}

CALLS: list[dict] = []  # {"path": str, "token": str | None}


def _workspace(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
    CALLS.append({"path": request.url.path, "token": token})
    return WORKSPACES.get(token)


async def conversations_list(request: Request):
    ws = _workspace(request)
    if ws is None:
        return JSONResponse({"ok": False, "error": "invalid_auth"})
    return JSONResponse({"ok": True, "channels": ws["channels"], "response_metadata": {"next_cursor": ""}})


async def conversations_history(request: Request):
    ws = _workspace(request)
    if request.query_params.get("channel") == "CREDIRECT":
        return RedirectResponse("/trap", status_code=302)
    if ws is None:
        return JSONResponse({"ok": False, "error": "invalid_auth"})
    return JSONResponse({"ok": True, "messages": ws["messages"]})


async def trap(request: Request):
    _workspace(request)
    return JSONResponse({"ok": True, "messages": []})


async def chat_post_message(request: Request):
    ws = _workspace(request)
    if ws is None:
        return JSONResponse({"ok": False, "error": "invalid_auth"})
    body = await request.json()
    return JSONResponse({"ok": True, "channel": body["channel"], "ts": "99.0"})


async def rate_limited(request: Request):
    _workspace(request)
    return JSONResponse({"ok": False, "error": "ratelimited"}, status_code=429, headers={"Retry-After": "7"})


app = Starlette(
    routes=[
        Route("/conversations.list", conversations_list),
        Route("/conversations.history", conversations_history),
        Route("/chat.postMessage", chat_post_message, methods=["POST"]),
        Route("/users.list", rate_limited),
        Route("/trap", trap, methods=["GET", "POST"]),
    ]
)


def start(port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server
