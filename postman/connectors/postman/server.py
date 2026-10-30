"""Postman MCP connector: streamable HTTP, stateless, multi-tenant.

Every caller sends their own Postman API key on every request:

  X-Api-Key: PMAK-...            (or 'Authorization: Bearer PMAK-...')
  X-Postman-Region: eu           (optional; default is the US API)

The server holds no credentials and no per-user state. The Postman API host is
fixed by the operator (US or EU), never supplied by the caller.
"""

import os
import re
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from connector_core.auth import get_api_key, get_optional_header
from connector_core.http import make_client

API_BASES = {"us": "https://api.postman.com", "eu": "https://api.eu.postman.com"}
# Operator-level override, used only by tests to point at a fake Postman.
API_BASE_OVERRIDE = os.environ.get("POSTMAN_API_BASE")

SCHEMA_V21 = "https://schema.postman.com/json/collection/v2.1.0/collection.json"
SECRET_MASK = "********"
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")

mcp = FastMCP(
    "postman",
    instructions=(
        "Postman access: workspaces, collections and environments. Send your own "
        "Postman API key on every request as 'X-Api-Key: <key>'. Add "
        "'X-Postman-Region: eu' if your account is on Postman's EU servers."
    ),
)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _base_url() -> str:
    if API_BASE_OVERRIDE:
        return API_BASE_OVERRIDE
    region = (get_optional_header("x-postman-region") or "us").lower()
    if region not in API_BASES:
        raise ToolError("X-Postman-Region must be 'us' or 'eu'.")
    return API_BASES[region]


def _id(value: str, what: str) -> str:
    """IDs go into URL paths, so only plain ID characters are allowed."""
    v = value.strip()
    if not _ID_RE.fullmatch(v):
        raise ToolError(f"Invalid {what} ID.")
    return quote(v, safe="-")


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("name") or "")
    if isinstance(err, str):
        return err
    return str(data.get("detail") or data.get("title") or "")


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if 300 <= status < 400:
        raise ToolError(f"Postman answered with a redirect (HTTP {status}). Redirects are not followed, so credentials never go to another address.")
    if status < 400:
        return
    if status == 401:
        raise ToolError("Postman rejected the API key (HTTP 401).")
    if status == 429:
        retry = (
            resp.headers.get("retryafter")
            or resp.headers.get("x-ratelimit-retryafter")
            or resp.headers.get("retry-after")
        )
        raise ToolError(
            "Postman rate limit reached (HTTP 429)." + (f" Retry after {retry} seconds." if retry else "")
        )
    detail = _error_detail(resp)
    if not detail and status == 404:
        detail = "not found"
    raise ToolError(f"Postman returned HTTP {status}" + (f" ({detail[:300]})" if detail else "") + ".")


async def _postman(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Call the Postman API as the current caller."""
    headers = {"X-Api-Key": get_api_key(), "Accept": "application/json"}
    async with make_client(_base_url(), headers) as client:
        try:
            resp = await client.request(method, path, params=params, json=body)
        except httpx.TimeoutException:
            raise ToolError("Postman API timed out.") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not reach the Postman API ({type(exc).__name__}).") from None
    _raise_for_status(resp)
    try:
        data = resp.json()
    except ValueError:
        raise ToolError("Postman returned a response that is not JSON.") from None
    return data if isinstance(data, dict) else {}


def _url_text(url: Any) -> str | None:
    if isinstance(url, str):
        return url
    if isinstance(url, dict):
        return url.get("raw")
    return None


def _flatten(items: list[dict[str, Any]], folder: str = "") -> list[dict[str, Any]]:
    """Walk folders and return one row per request."""
    rows: list[dict[str, Any]] = []
    for it in items or []:
        name = it.get("name")
        if isinstance(it.get("item"), list):  # a folder
            rows.extend(_flatten(it["item"], f"{folder}/{name}" if folder else str(name)))
        elif isinstance(it.get("request"), dict):
            req = it["request"]
            rows.append({
                "id": it.get("id"),
                "name": name,
                "folder": folder or None,
                "method": req.get("method", "GET"),
                "url": _url_text(req.get("url")),
            })
    return rows


# ---------------------------------------------------------------- read tools


@mcp.tool(annotations=READ_ONLY)
async def get_me() -> dict[str, Any]:
    """Who the API key belongs to (username, team) and the account's API usage."""
    d = await _postman("GET", "/me")
    u = d.get("user", {})
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "full_name": u.get("fullName"),
        "team_name": u.get("teamName"),
        "usage": [
            {"name": o.get("name"), "usage": o.get("usage"), "limit": o.get("limit")}
            for o in d.get("operations", [])
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def list_workspaces() -> dict[str, Any]:
    """List the workspaces the caller can see."""
    d = await _postman("GET", "/workspaces")
    return {
        "workspaces": [
            {"id": w.get("id"), "name": w.get("name"), "type": w.get("type"), "visibility": w.get("visibility")}
            for w in d.get("workspaces", [])
        ]
    }


@mcp.tool(annotations=READ_ONLY)
async def get_workspace(
    workspace_id: Annotated[str, Field(description="Workspace ID from list_workspaces")],
) -> dict[str, Any]:
    """Get a workspace with the collections and environments in it."""
    w = (await _postman("GET", f"/workspaces/{_id(workspace_id, 'workspace')}")).get("workspace", {})
    return {
        "id": w.get("id"),
        "name": w.get("name"),
        "type": w.get("type"),
        "description": w.get("description"),
        "collections": [{"id": c.get("id"), "name": c.get("name")} for c in w.get("collections", []) or []],
        "environments": [{"id": e.get("id"), "name": e.get("name")} for e in w.get("environments", []) or []],
    }


@mcp.tool(annotations=READ_ONLY)
async def list_collections(
    workspace_id: Annotated[str | None, Field(description="Only this workspace (optional)")] = None,
) -> dict[str, Any]:
    """List collections, optionally only those in one workspace."""
    params = {"workspace": _id(workspace_id, "workspace")} if workspace_id else None
    d = await _postman("GET", "/collections", params=params)
    return {
        "collections": [
            {"id": c.get("id"), "uid": c.get("uid"), "name": c.get("name"), "updated_at": c.get("updatedAt")}
            for c in d.get("collections", [])
        ]
    }


@mcp.tool(annotations=READ_ONLY)
async def get_collection(
    collection_id: Annotated[str, Field(description="Collection ID from list_collections")],
    include_raw: Annotated[bool, Field(description="Also return the full collection JSON (can be large)")] = False,
) -> dict[str, Any]:
    """Get a collection: its name, description and every request (method, URL, folder)."""
    c = (await _postman("GET", f"/collections/{_id(collection_id, 'collection')}")).get("collection", {})
    info = c.get("info", {})
    out: dict[str, Any] = {
        "id": info.get("_postman_id"),
        "name": info.get("name"),
        "description": info.get("description"),
        "requests": _flatten(c.get("item", [])),
    }
    if include_raw:
        out["raw"] = c
    return out


@mcp.tool(annotations=READ_ONLY)
async def list_environments(
    workspace_id: Annotated[str | None, Field(description="Only this workspace (optional)")] = None,
) -> dict[str, Any]:
    """List environments, optionally only those in one workspace."""
    params = {"workspace": _id(workspace_id, "workspace")} if workspace_id else None
    d = await _postman("GET", "/environments", params=params)
    return {
        "environments": [
            {"id": e.get("id"), "uid": e.get("uid"), "name": e.get("name"), "updated_at": e.get("updatedAt")}
            for e in d.get("environments", [])
        ]
    }


@mcp.tool(annotations=READ_ONLY)
async def get_environment(
    environment_id: Annotated[str, Field(description="Environment ID from list_environments")],
) -> dict[str, Any]:
    """Get an environment's variables. Values of 'secret' variables are always masked."""
    e = (await _postman("GET", f"/environments/{_id(environment_id, 'environment')}")).get("environment", {})
    return {
        "id": e.get("id"),
        "name": e.get("name"),
        "variables": [
            {
                "key": v.get("key"),
                "value": SECRET_MASK if v.get("type") == "secret" else v.get("value"),
                "type": v.get("type", "default"),
                "enabled": v.get("enabled", True),
            }
            for v in e.get("values", []) or []
        ],
    }


# --------------------------------------------------------------- write tools


class RequestSpec(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    url: Annotated[str, Field(min_length=1, max_length=2000, description="e.g. https://api.example.com/users or {{baseUrl}}/users")]
    headers: dict[str, str] | None = None
    body: Annotated[str | None, Field(description="Raw body text, e.g. a JSON string")] = None


@mcp.tool(annotations=WRITE)
async def create_collection(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    requests: Annotated[list[RequestSpec], Field(max_length=100, description="Requests to put in the collection")],
    workspace_id: Annotated[str | None, Field(description="Target workspace; omitted means your default personal workspace")] = None,
    description: Annotated[str | None, Field(max_length=5000)] = None,
) -> dict[str, Any]:
    """Create a collection containing the given requests."""
    items = []
    for r in requests:
        req: dict[str, Any] = {"method": r.method, "url": r.url}
        if r.headers:
            req["header"] = [{"key": k, "value": v} for k, v in r.headers.items()]
        if r.body is not None:
            req["body"] = {"mode": "raw", "raw": r.body}
        items.append({"name": r.name, "request": req})
    info: dict[str, Any] = {"name": name, "schema": SCHEMA_V21}
    if description:
        info["description"] = description
    params = {"workspace": _id(workspace_id, "workspace")} if workspace_id else None
    d = await _postman("POST", "/collections", params=params, body={"collection": {"info": info, "item": items}})
    c = d.get("collection", {})
    return {"id": c.get("id"), "uid": c.get("uid"), "name": c.get("name")}


@mcp.tool(annotations=WRITE)
async def create_environment(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    variables: Annotated[dict[str, str], Field(description='e.g. {"baseUrl": "https://api.example.com"}')],
    secret_keys: Annotated[list[str] | None, Field(description="Which variable keys to store as 'secret' (masked in Postman)")] = None,
    workspace_id: Annotated[str | None, Field(description="Target workspace; omitted means your default personal workspace")] = None,
) -> dict[str, Any]:
    """Create an environment with the given variables."""
    secrets = set(secret_keys or [])
    unknown = secrets - set(variables)
    if unknown:
        raise ToolError(f"secret_keys not in variables: {', '.join(sorted(unknown))}")
    values = [
        {"key": k, "value": v, "type": "secret" if k in secrets else "default", "enabled": True}
        for k, v in variables.items()
    ]
    params = {"workspace": _id(workspace_id, "workspace")} if workspace_id else None
    d = await _postman("POST", "/environments", params=params, body={"environment": {"name": name, "values": values}})
    e = d.get("environment", {})
    return {"id": e.get("id"), "uid": e.get("uid"), "name": e.get("name")}


@mcp.tool(annotations=WRITE)
async def add_request(
    collection_id: Annotated[str, Field(description="Collection ID from list_collections")],
    name: Annotated[str, Field(min_length=1, max_length=200)],
    url: Annotated[str, Field(min_length=1, max_length=2000, description="e.g. https://api.example.com/orders or {{baseUrl}}/orders")],
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET",
    headers: Annotated[dict[str, str] | None, Field(description="Request headers")] = None,
    body: Annotated[str | None, Field(description="Raw body text, e.g. a JSON string")] = None,
    folder_id: Annotated[str | None, Field(description="Put it in this folder of the collection (optional)")] = None,
    description: Annotated[str | None, Field(max_length=5000)] = None,
) -> dict[str, Any]:
    """Add one request to an existing collection. Nothing else in the collection changes."""
    payload: dict[str, Any] = {"name": name, "method": method, "url": url}
    if description:
        payload["description"] = description
    if headers:
        payload["headerData"] = [{"key": k, "value": v} for k, v in headers.items()]
    if body is not None:
        payload["dataMode"] = "raw"
        payload["rawModeData"] = body
    params = {"folder": _id(folder_id, "folder")} if folder_id else None
    d = await _postman("POST", f"/collections/{_id(collection_id, 'collection')}/requests", params=params, body=payload)
    data = d.get("data") or {}
    return {"id": data.get("id") or d.get("model_id"), "name": data.get("name", name), "collection_id": collection_id}


@mcp.tool(annotations=DESTRUCTIVE)
async def update_environment(
    environment_id: Annotated[str, Field(description="Environment ID from list_environments")],
    set_variables: Annotated[dict[str, str] | None, Field(description='Variables to change or add, e.g. {"baseUrl": "https://staging.example.com"}')] = None,
    remove_keys: Annotated[list[str] | None, Field(description="Variable keys to delete")] = None,
    secret_keys: Annotated[list[str] | None, Field(description="Keys from set_variables to store as 'secret' (masked)")] = None,
) -> dict[str, Any]:
    """Change, add or remove variables in an existing environment.

    Only the variables you name are touched; every other variable (including
    secrets) stays exactly as it is. Values are never echoed back.
    """
    eid = _id(environment_id, "environment")
    to_set = set_variables or {}
    to_remove = list(dict.fromkeys(remove_keys or []))
    secrets_wanted = set(secret_keys or [])
    if not to_set and not to_remove:
        raise ToolError("Nothing to change: give set_variables and/or remove_keys.")
    both = set(to_set) & set(to_remove)
    if both:
        raise ToolError(f"Keys cannot be both set and removed: {', '.join(sorted(both))}")
    if secrets_wanted - set(to_set):
        raise ToolError("secret_keys must be keys in set_variables.")

    env = (await _postman("GET", f"/environments/{eid}")).get("environment", {})
    values = env.get("values") or []
    keys = [v.get("key") for v in values]
    involved = set(to_set) | set(to_remove)
    dupes = sorted(k for k in involved if keys.count(k) > 1)
    if dupes:
        raise ToolError(f"The environment has duplicate keys, so the change would be ambiguous: {', '.join(dupes)}")
    missing = [k for k in to_remove if k not in keys]
    if missing:
        raise ToolError(f"Not in the environment: {', '.join(missing)}")

    # Postman applies one kind of operation per request, so changes go in up to three
    # targeted PATCH calls: replace existing values, append new ones, then remove
    # (highest index first, so earlier removals don't shift later ones).
    replace_ops, add_ops = [], []
    updated, added = [], []
    for key, value in to_set.items():
        if key in keys:
            i = keys.index(key)
            replace_ops.append({"op": "replace", "path": f"/values/{i}/value", "value": value})
            if key in secrets_wanted and values[i].get("type") != "secret":
                replace_ops.append({"op": "replace", "path": f"/values/{i}/type", "value": "secret"})
            updated.append(key)
        else:
            add_ops.append({"op": "add", "path": f"/values/{len(values) + len(add_ops)}", "value": {
                "key": key, "value": value, "type": "secret" if key in secrets_wanted else "default", "enabled": True}})
            added.append(key)
    remove_ops = [{"op": "remove", "path": f"/values/{keys.index(k)}"}
                  for k in sorted(to_remove, key=keys.index, reverse=True)]
    calls = 0
    for ops in (replace_ops, add_ops, remove_ops):
        if ops:
            await _postman("PATCH", f"/environments/{eid}", body=ops)
            calls += 1
    return {"id": eid, "updated": updated, "added": added, "removed": to_remove, "patch_calls": calls}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_collection(
    collection_id: Annotated[str, Field(description="Collection ID")],
) -> dict[str, Any]:
    """Delete a collection. This cannot be undone."""
    d = await _postman("DELETE", f"/collections/{_id(collection_id, 'collection')}")
    return {"deleted": True, "id": d.get("collection", {}).get("id")}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_environment(
    environment_id: Annotated[str, Field(description="Environment ID")],
) -> dict[str, Any]:
    """Delete an environment. This cannot be undone."""
    d = await _postman("DELETE", f"/environments/{_id(environment_id, 'environment')}")
    return {"deleted": True, "id": d.get("environment", {}).get("id")}


def main() -> None:
    mcp.run(
        transport="http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
