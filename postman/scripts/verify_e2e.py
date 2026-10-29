"""End-to-end check: do the connector's actions really happen in Postman?

For each step, this script does something THROUGH the connector, then asks the
Postman API DIRECTLY (not through the connector) whether it happened. It
creates a test collection and a test environment named "MCP verify <random>",
pauses so you can see them in the Postman app, then deletes both.

Needs the connector running and a Postman API key. Usage:
  PowerShell:  $env:POSTMAN_API_KEY = "PMAK-..."
  cmd:         set POSTMAN_API_KEY=PMAK-...
  then:        uv run python scripts/verify_e2e.py [connector-url] [--no-pause]

Optional:
  POSTMAN_WORKSPACE_ID   which workspace to use (default: your first personal one)
  POSTMAN_REGION=eu      if your account is on Postman's EU servers
Uses about 25 Postman API calls (free plans have a monthly allowance).
"""

import asyncio
import os
import secrets
import sys

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

API_BASES = {"us": "https://api.postman.com", "eu": "https://api.eu.postman.com"}
RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))


def expect(cond: bool, name: str, detail: str = "") -> bool:
    record("PASS" if cond else "FAIL", name, detail)
    return cond


def summary() -> int:
    count = {s: len([r for r in RESULTS if r[0] == s]) for s in ("PASS", "FAIL", "SKIP", "WARN")}
    print(f"\nSummary: {count['PASS']} passed, {count['FAIL']} failed, "
          f"{count['SKIP']} skipped, {count['WARN']} warnings")
    return 1 if count["FAIL"] else 0


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pause = "--no-pause" not in sys.argv and sys.stdin.isatty()
    connector_url = args[0] if args else "http://127.0.0.1:8000/mcp"
    key = os.environ.get("POSTMAN_API_KEY")
    if not key:
        sys.exit("Set the POSTMAN_API_KEY environment variable first.")
    region = os.environ.get("POSTMAN_REGION", "us").lower()
    if region not in API_BASES:
        sys.exit("POSTMAN_REGION must be 'us' or 'eu'.")
    # POSTMAN_API_BASE only changes in tests, where Postman is replaced by a stand-in.
    api_base = os.environ.get("POSTMAN_API_BASE") or API_BASES[region]

    tag = secrets.token_hex(4)
    col_name, env_name = f"MCP verify {tag}", f"MCP verify env {tag}"
    print(f"Test collection: {col_name!r}   test environment: {env_name!r}\n")

    # Preflight: is the connector running, and does the key work?
    health_url = connector_url.rsplit("/mcp", 1)[0] + "/health"
    try:
        up = httpx.get(health_url, timeout=5).text == "ok"
    except httpx.HTTPError:
        up = False
    if not up:
        print(f"[FAIL] The connector is not answering at {health_url}.")
        print("       Start it first (other window): uv run python -m connectors.postman.server")
        return 1

    direct = httpx.AsyncClient(base_url=api_base, headers={"X-Api-Key": key}, timeout=20)
    try:
        me = await direct.get("/me")
    except httpx.HTTPError as exc:
        print(f"[FAIL] Could not reach the Postman API at {api_base} ({type(exc).__name__}).")
        await direct.aclose()
        return 1
    if me.status_code == 401:
        print("[FAIL] Postman rejected POSTMAN_API_KEY (HTTP 401). Check you copied the whole key.")
        await direct.aclose()
        return 1
    if me.status_code != 200:
        print(f"[FAIL] Postman answered HTTP {me.status_code} to /me.")
        await direct.aclose()
        return 1
    real_user = me.json().get("user", {})

    headers = {"X-Api-Key": key}
    if region == "eu":
        headers["X-Postman-Region"] = "eu"
    created: dict[str, str] = {}  # "collection"/"environment" -> id, for cleanup

    async with Client(StreamableHttpTransport(connector_url, headers=headers), timeout=30) as mcp:

        async def tool(name: str, params: dict | None = None):
            return (await mcp.call_tool(name, params or {})).data

        try:
            # 1. Identity: the connector acts as the key's owner.
            via = await tool("get_me")
            expect(via["username"] == real_user.get("username"), "get_me matches Postman's own answer",
                   f"user {real_user.get('username')}")

            # 2. Pick a workspace and check the listing matches Postman's.
            ws_via = (await tool("list_workspaces"))["workspaces"]
            ws_real = (await direct.get("/workspaces")).json().get("workspaces", [])
            expect({w["id"] for w in ws_via} == {w["id"] for w in ws_real}, "list_workspaces matches Postman",
                   f"{len(ws_real)} workspace(s)")
            ws_id = os.environ.get("POSTMAN_WORKSPACE_ID") or next(
                (w["id"] for w in ws_real if w.get("type") == "personal"), ws_real[0]["id"] if ws_real else None)
            ws_name = next((w["name"] for w in ws_real if w["id"] == ws_id), "?")
            if not ws_id:
                record("FAIL", "no workspace found for this key")
                return summary()
            print(f"Using workspace: {ws_name!r}\n")

            # 3. Create a collection through the connector, confirm directly.
            col = await tool("create_collection", {
                "name": col_name,
                "workspace_id": ws_id,
                "description": "Created by the MCP connector end-to-end check. Safe to delete.",
                "requests": [
                    {"name": "Echo GET", "method": "GET", "url": f"https://postman-echo.com/get?check={tag}"},
                    {"name": "Echo POST", "method": "POST", "url": "https://postman-echo.com/post",
                     "headers": {"Content-Type": "application/json"}, "body": f'{{"check": "{tag}"}}'},
                ],
            })
            created["collection"] = col["id"]
            record("PASS", "create_collection returned an ID", col["id"])
            r = await direct.get(f"/collections/{col['id']}")
            real_col = r.json().get("collection", {}) if r.status_code == 200 else {}
            real_names = [i.get("name") for i in real_col.get("item", [])]
            expect(real_col.get("info", {}).get("name") == col_name and real_names == ["Echo GET", "Echo POST"],
                   "collection really exists in Postman with both requests (direct check)")

            # 4. Read it back through the connector.
            got = await tool("get_collection", {"collection_id": col["id"]})
            expect([(q["name"], q["method"]) for q in got["requests"]] == [("Echo GET", "GET"), ("Echo POST", "POST")],
                   "get_collection returns both requests")
            listed = [c["id"] for c in (await tool("list_collections", {"workspace_id": ws_id}))["collections"]]
            expect(col["id"] in listed, "list_collections shows it in the workspace")

            # 5. Environment with a secret, confirm directly, check the connector masks it.
            fake_secret = f"not-a-real-secret-{tag}"
            env = await tool("create_environment", {
                "name": env_name,
                "workspace_id": ws_id,
                "variables": {"baseUrl": "https://postman-echo.com", "token": fake_secret},
                "secret_keys": ["token"],
            })
            created["environment"] = env["id"]
            record("PASS", "create_environment returned an ID", env["id"])
            r = await direct.get(f"/environments/{env['id']}")
            real_env = r.json().get("environment", {}) if r.status_code == 200 else {}
            types = {v.get("key"): v.get("type") for v in real_env.get("values", [])}
            expect(real_env.get("name") == env_name and types == {"baseUrl": "default", "token": "secret"},
                   "environment really exists in Postman, token stored as secret (direct check)")
            vars_via = {v["key"]: v["value"] for v in (await tool("get_environment", {"environment_id": env["id"]}))["variables"]}
            expect(vars_via.get("baseUrl") == "https://postman-echo.com" and vars_via.get("token") == "********",
                   "get_environment shows baseUrl and masks the secret")
            listed = [e["id"] for e in (await tool("list_environments", {"workspace_id": ws_id}))["environments"]]
            expect(env["id"] in listed, "list_environments shows it in the workspace")
            ws = await tool("get_workspace", {"workspace_id": ws_id})
            expect(col["id"] in [c["id"] for c in ws["collections"]] and env["id"] in [e["id"] for e in ws["environments"]],
                   "get_workspace lists both")

            # 6. Eyes-on moment.
            print("\nLOOK NOW: open the Postman app (or go.postman.co in your browser).")
            print(f"In workspace {ws_name!r} you should see:")
            print(f"  Collections  -> {col_name!r} with requests 'Echo GET' and 'Echo POST'")
            print(f"  Environments -> {env_name!r} with baseUrl, and token shown as a masked secret")
            print("Optional: open 'Echo GET' and click Send; Postman Echo answers with your check value.")
            print("Take a screenshot.")
            if pause:
                input("Press Enter to continue (both are deleted next)... ")

            # 7. Delete through the connector, confirm really gone.
            await tool("delete_collection", {"collection_id": col["id"]})
            r = await direct.get(f"/collections/{col['id']}")
            if expect(r.status_code == 404, "collection is really gone from Postman (direct check)", f"HTTP {r.status_code}"):
                created.pop("collection")
            await tool("delete_environment", {"environment_id": env["id"]})
            r = await direct.get(f"/environments/{env['id']}")
            if expect(r.status_code == 404, "environment is really gone from Postman (direct check)", f"HTTP {r.status_code}"):
                created.pop("environment")
        except ToolError as exc:
            record("FAIL", "connector call raised an error", str(exc))
        except httpx.HTTPError as exc:
            record("FAIL", "direct request to Postman failed", type(exc).__name__)
        finally:
            # Clean up anything this script created that is still there.
            for kind, id_ in list(created.items()):
                try:
                    r = await direct.delete(f"/{kind}s/{id_}")
                    ok = r.status_code in (200, 404)
                except httpx.HTTPError:
                    ok = False
                record("INFO" if ok else "WARN",
                       f"cleanup: test {kind} deleted directly" if ok else f"could not delete test {kind}; delete it by hand",
                       id_)
            await direct.aclose()

    return summary()


sys.exit(asyncio.run(main()))
