"""End-to-end multi-tenant tests for the Postman connector.

The real connector runs as a subprocess over streamable HTTP and talks to a
fake Postman API with two separate accounts. We check that each caller's key
reaches Postman for that caller's calls only, even when many callers hit the
server at once, and that writes land in the caller's own account.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from tests import fake_postman as fake

ROOT = Path(__file__).resolve().parent.parent
FAKE_PORT, CONNECTOR_PORT = 8861, 8862
URL = f"http://127.0.0.1:{CONNECTOR_PORT}/mcp"
KEY_A, KEY_B = "PMAK-user-a", "PMAK-user-b"


@pytest.fixture(scope="module")
def connector(tmp_path_factory):
    fake.reset()
    server = fake.start(FAKE_PORT)
    env = {
        **os.environ,
        "POSTMAN_API_BASE": f"http://127.0.0.1:{FAKE_PORT}",
        "HOST": "127.0.0.1",
        "PORT": str(CONNECTOR_PORT),
    }
    # Server output goes to a FILE, not a pipe. A pipe nobody reads fills up
    # (about 4 KB on Windows), which blocks the server and hangs every test.
    log_path = tmp_path_factory.mktemp("logs") / "connector.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "connectors.postman.server"],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            log_file.close()
            pytest.fail(f"connector exited early:\n{log_path.read_text(errors='replace')}")
        try:
            if httpx.get(f"http://127.0.0.1:{CONNECTOR_PORT}/health", timeout=1).text == "ok":
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        log_file.close()
        pytest.fail(f"connector did not start:\n{log_path.read_text(errors='replace')}")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log_file.close()
    server.should_exit = True
    log = log_path.read_text(errors="replace")
    # Secrets must never appear in server logs.
    assert KEY_A not in log and KEY_B not in log and "s3cret-value" not in log


def _client(headers: dict[str, str]) -> Client:
    # A timeout makes a stuck call fail loudly instead of hanging forever.
    return Client(StreamableHttpTransport(URL, headers=headers), timeout=20)


def _key(key: str) -> dict[str, str]:
    return {"X-Api-Key": key}


async def _call(headers: dict[str, str], tool: str, args: dict | None = None):
    async with _client(headers) as c:
        return await c.call_tool(tool, args or {})


def run(coro):
    return asyncio.run(coro)


def _workspace_id(key: str) -> str:
    return run(_call(_key(key), "list_workspaces")).data["workspaces"][0]["id"]


def test_tools_are_listed_with_correct_hints(connector):
    async def go():
        async with _client(_key(KEY_A)) as c:
            return {t.name: t for t in await c.list_tools()}

    tools = run(go())
    assert {
        "get_me", "list_workspaces", "get_workspace", "list_collections", "get_collection",
        "list_environments", "get_environment", "create_collection", "create_environment",
        "delete_collection", "delete_environment",
    } <= set(tools)
    assert tools["get_collection"].annotations.read_only_hint is True
    assert tools["create_collection"].annotations.read_only_hint is False
    assert tools["delete_collection"].annotations.destructive_hint is True
    assert tools["delete_environment"].annotations.destructive_hint is True


def test_each_caller_sees_only_their_own_account(connector):
    assert run(_call(_key(KEY_A), "get_me")).data["username"] == "alice"
    assert run(_call(_key(KEY_B), "get_me")).data["username"] == "bob"
    assert run(_call(_key(KEY_A), "list_workspaces")).data["workspaces"][0]["name"] == "Alice Workspace"
    assert run(_call(_key(KEY_B), "list_workspaces")).data["workspaces"][0]["name"] == "Bob Workspace"


def test_bearer_header_is_accepted_too(connector):
    r = run(_call({"Authorization": f"Bearer {KEY_B}"}, "get_me")).data
    assert r["username"] == "bob"


def test_concurrent_callers_never_cross(connector):
    async def one(key):
        return (await _call(_key(key), "get_me")).data["username"]

    async def go():
        jobs = []
        for _ in range(15):
            jobs.append(one(KEY_A))
            jobs.append(one(KEY_B))
        return await asyncio.gather(*jobs)

    for i, name in enumerate(run(go())):
        assert name == ("alice" if i % 2 == 0 else "bob")


def test_create_collection_lands_in_callers_account_only(connector):
    ws_a = _workspace_id(KEY_A)
    fake.CALLS.clear()
    created = run(_call(_key(KEY_A), "create_collection", {
        "name": "Orders API",
        "workspace_id": ws_a,
        "description": "test",
        "requests": [
            {"name": "List orders", "method": "GET", "url": "https://api.example.com/orders"},
            {"name": "Create order", "method": "POST", "url": "https://api.example.com/orders",
             "headers": {"Content-Type": "application/json"}, "body": '{"qty": 1}'},
        ],
    })).data
    # What reached Postman: the caller's key, the right workspace, a v2.1 collection.
    post = [c for c in fake.CALLS if c["method"] == "POST" and c["path"] == "/collections"][0]
    assert post["key"] == KEY_A and post["query"] == {"workspace": ws_a}
    sent = post["body"]["collection"]
    assert sent["info"]["schema"].endswith("/v2.1.0/collection.json")
    assert sent["item"][1]["request"]["body"] == {"mode": "raw", "raw": '{"qty": 1}'}
    assert sent["item"][1]["request"]["header"] == [{"key": "Content-Type", "value": "application/json"}]
    # Readable back through the connector, by the owner only.
    got = run(_call(_key(KEY_A), "get_collection", {"collection_id": created["id"]})).data
    assert got["name"] == "Orders API"
    assert [(r["name"], r["method"]) for r in got["requests"]] == [("List orders", "GET"), ("Create order", "POST")]
    assert created["id"] in [c["id"] for c in run(_call(_key(KEY_A), "list_collections", {"workspace_id": ws_a})).data["collections"]]
    with pytest.raises(ToolError, match="404"):
        run(_call(_key(KEY_B), "get_collection", {"collection_id": created["id"]}))
    assert run(_call(_key(KEY_B), "list_collections")).data["collections"] == []
    # Delete, then it is gone.
    assert run(_call(_key(KEY_A), "delete_collection", {"collection_id": created["id"]})).data["deleted"] is True
    with pytest.raises(ToolError, match="not found|404"):
        run(_call(_key(KEY_A), "get_collection", {"collection_id": created["id"]}))


def test_folders_are_flattened_with_their_path(connector):
    acct = fake.ACCOUNTS[KEY_A]
    ws = next(iter(acct["workspaces"]))
    acct["collections"]["11111111-aaaa-bbbb-cccc-222222222222"] = {"workspace": ws, "collection": {
        "info": {"_postman_id": "11111111-aaaa-bbbb-cccc-222222222222", "name": "Nested"},
        "item": [{"name": "Users", "item": [{"name": "Admins", "item": [
            {"name": "Get admin", "request": {"method": "GET", "url": {"raw": "https://x/admins/1"}}}]}]}],
    }}
    got = run(_call(_key(KEY_A), "get_collection", {"collection_id": "11111111-aaaa-bbbb-cccc-222222222222"})).data
    assert got["requests"] == [{"id": None, "name": "Get admin", "folder": "Users/Admins", "method": "GET",
                                "url": "https://x/admins/1"}]


def test_environment_secrets_are_masked(connector):
    created = run(_call(_key(KEY_B), "create_environment", {
        "name": "Staging",
        "variables": {"baseUrl": "https://staging.example.com", "token": "s3cret-value"},
        "secret_keys": ["token"],
    })).data
    sent = [c for c in fake.CALLS if c["method"] == "POST" and c["path"] == "/environments"][-1]["body"]
    assert {"key": "token", "value": "s3cret-value", "type": "secret", "enabled": True} in sent["environment"]["values"]
    got = run(_call(_key(KEY_B), "get_environment", {"environment_id": created["id"]})).data
    values = {v["key"]: v["value"] for v in got["variables"]}
    assert values == {"baseUrl": "https://staging.example.com", "token": "********"}
    assert created["id"] in [e["id"] for e in run(_call(_key(KEY_B), "list_environments")).data["environments"]]
    ws = run(_call(_key(KEY_B), "get_workspace", {"workspace_id": _workspace_id(KEY_B)})).data
    assert created["id"] in [e["id"] for e in ws["environments"]]
    assert run(_call(_key(KEY_B), "delete_environment", {"environment_id": created["id"]})).data["deleted"] is True


def test_secret_keys_must_exist(connector):
    with pytest.raises(ToolError, match="secret_keys not in variables"):
        run(_call(_key(KEY_A), "create_environment", {"name": "x", "variables": {"a": "1"}, "secret_keys": ["b"]}))


def test_missing_key_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Missing Postman API key"):
        run(_call({}, "get_me"))


def test_malformed_bearer_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Bearer"):
        run(_call({"Authorization": "Token abc"}, "get_me"))


def test_wrong_key_is_rejected_without_leaking_it(connector):
    with pytest.raises(ToolError) as exc:
        run(_call(_key("PMAK-not-real-123"), "get_me"))
    assert "rejected the API key" in str(exc.value)
    assert "PMAK-not-real-123" not in str(exc.value)


def test_rate_limit_is_reported_with_retry_after(connector):
    with pytest.raises(ToolError, match="Retry after 7"):
        run(_call(_key("PMAK-ratelimited"), "get_me"))


def test_bad_ids_are_refused_before_reaching_postman(connector):
    fake.CALLS.clear()
    for bad in ["../me", "abc/def", "a b", "x" * 101]:
        with pytest.raises(ToolError, match="Invalid collection ID"):
            run(_call(_key(KEY_A), "get_collection", {"collection_id": bad}))
    assert fake.CALLS == []


def test_region_header_picks_a_fixed_host(monkeypatch):
    # Callers can only choose between the two fixed Postman hosts, never a URL.
    import connectors.postman.server as s

    monkeypatch.setattr(s, "API_BASE_OVERRIDE", None)
    monkeypatch.setattr(s, "get_optional_header", lambda name: "mars")
    with pytest.raises(ToolError, match="'us' or 'eu'"):
        s._base_url()
    monkeypatch.setattr(s, "get_optional_header", lambda name: "EU")
    assert s._base_url() == "https://api.eu.postman.com"
    monkeypatch.setattr(s, "get_optional_header", lambda name: None)
    assert s._base_url() == "https://api.postman.com"
