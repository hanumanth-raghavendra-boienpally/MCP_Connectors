"""End-to-end multi-tenant tests for the Elasticsearch connector.

The real connector runs as a subprocess over streamable HTTP and talks to two
fake Elasticsearch clusters. We check that each caller's URL and API key reach
that caller's cluster only, even when many callers hit the server at once.
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

from tests import fake_elasticsearch as fake

ROOT = Path(__file__).resolve().parent.parent
PORT_A, PORT_B, CONNECTOR_PORT = 8801, 8802, 8803
URL = f"http://127.0.0.1:{CONNECTOR_PORT}/mcp"
CLUSTERS = {
    "a": {"url": f"http://127.0.0.1:{PORT_A}", "key": "key-a-secret"},
    "b": {"url": f"http://127.0.0.1:{PORT_B}", "key": "key-b-secret"},
}


@pytest.fixture(scope="module")
def connector(tmp_path_factory):
    servers = [
        fake.start(
            fake.make_app("cluster-a", CLUSTERS["a"]["key"], [
                {"_id": "1", "_source": {"level": "error", "msg": "a-one"}},
                {"_id": "2", "_source": {"level": "info", "msg": "a-two"}},
            ]),
            PORT_A,
        ),
        fake.start(
            fake.make_app("cluster-b", CLUSTERS["b"]["key"], [
                {"_id": "9", "_source": {"level": "error", "msg": "b-one"}},
            ]),
            PORT_B,
        ),
    ]
    env = {
        **os.environ,
        # The operator allowlist: only this host may be used as a cluster URL.
        "ES_ALLOWED_HOSTS": "127.0.0.1",
        "HOST": "127.0.0.1",
        "PORT": str(CONNECTOR_PORT),
    }
    # Server output goes to a FILE, not a pipe. A pipe nobody reads fills up
    # (about 4 KB on Windows), which blocks the server and hangs every test.
    log_path = tmp_path_factory.mktemp("logs") / "connector.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "connectors.elasticsearch.server"],
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
    for s in servers:
        s.should_exit = True
    log = log_path.read_text(errors="replace")
    # Secrets must never appear in server logs.
    assert CLUSTERS["a"]["key"] not in log and CLUSTERS["b"]["key"] not in log


def _client(who: str | None, *, key: str | None = None, url: str | None = None, auth: str | None = None) -> Client:
    """A caller for cluster 'a' or 'b'. Overrides let tests send bad headers."""
    headers: dict[str, str] = {}
    if who is not None:
        headers["X-Elasticsearch-URL"] = CLUSTERS[who]["url"]
        headers["Authorization"] = f"ApiKey {CLUSTERS[who]['key']}"
    if url is not None:
        headers["X-Elasticsearch-URL"] = url
    if key is not None:
        headers["Authorization"] = f"ApiKey {key}"
    if auth is not None:
        headers["Authorization"] = auth
    # A timeout makes a stuck call fail loudly instead of hanging forever.
    return Client(StreamableHttpTransport(URL, headers=headers), timeout=20)


async def _call(client: Client, tool: str, args: dict | None = None):
    async with client as c:
        return await c.call_tool(tool, args or {})


def run(coro):
    return asyncio.run(coro)


def test_tools_are_listed_with_correct_hints(connector):
    async def go():
        async with _client("a") as c:
            return {t.name: t for t in await c.list_tools()}

    tools = run(go())
    assert {
        "cluster_health", "list_indices", "get_mapping", "search", "count",
        "esql_query", "get_document", "index_document", "delete_document",
    } <= set(tools)
    assert tools["search"].annotations.read_only_hint is True
    assert tools["esql_query"].annotations.read_only_hint is True
    assert tools["index_document"].annotations.read_only_hint is False
    assert tools["delete_document"].annotations.destructive_hint is True


def test_each_caller_reaches_only_their_own_cluster(connector):
    a = run(_call(_client("a"), "cluster_health")).data
    b = run(_call(_client("b"), "cluster_health")).data
    assert a["cluster_name"] == "cluster-a" and b["cluster_name"] == "cluster-b"


def test_concurrent_callers_never_cross(connector):
    async def one(who):
        r = await _call(_client(who), "search", {"index": "logs"})
        return [h["source"]["msg"] for h in r.data["hits"]]

    async def go():
        jobs = []
        for _ in range(15):
            jobs.append(one("a"))
            jobs.append(one("b"))
        return await asyncio.gather(*jobs)

    for i, msgs in enumerate(run(go())):
        assert msgs == (["a-one", "a-two"] if i % 2 == 0 else ["b-one"])


def test_key_reaching_each_cluster_matches_the_caller(connector):
    fake.CALLS.clear()
    run(_call(_client("b"), "search", {"index": "logs"}))
    run(_call(_client("a"), "count", {"index": "logs"}))
    for call in fake.CALLS:
        expected = CLUSTERS["a" if call["cluster"] == "cluster-a" else "b"]["key"]
        assert call["auth"] == f"ApiKey {expected}"
    assert {c["cluster"] for c in fake.CALLS} == {"cluster-a", "cluster-b"}


def test_search_sends_query_and_returns_hits(connector):
    fake.CALLS.clear()
    r = run(_call(_client("a"), "search", {
        "index": "logs", "query": {"term": {"level": "error"}}, "size": 5, "fields": ["msg"],
    })).data
    assert r["total"] == 1 and r["hits"][0]["source"]["msg"] == "a-one"
    sent = [c for c in fake.CALLS if c["path"] == "/logs/_search"][0]["body"]
    assert sent["query"] == {"term": {"level": "error"}} and sent["size"] == 5 and sent["_source"] == ["msg"]


def test_list_indices_and_mapping_and_count(connector):
    idx = run(_call(_client("a"), "list_indices", {"pattern": "logs-*"})).data
    assert idx["indices"][0]["index"] == "cluster-a-logs" and idx["truncated"] is False
    mapping = run(_call(_client("a"), "get_mapping", {"index": "logs"})).data
    assert "logs" in mapping
    assert run(_call(_client("b"), "count", {"index": "logs"})).data["count"] == 1


def test_esql_rows_are_capped(connector):
    r = run(_call(_client("a"), "esql_query", {"query": "FROM logs", "max_rows": 2})).data
    assert r["columns"][0]["name"] == "cluster" and len(r["rows"]) == 2 and r["truncated"] is True


def test_write_tools_act_as_the_caller_on_their_cluster(connector):
    fake.CALLS.clear()
    created = run(_call(_client("a"), "index_document", {"index": "logs", "document": {"msg": "new"}, "doc_id": "n1"})).data
    assert created["result"] == "created"
    assert run(_call(_client("a"), "get_document", {"index": "logs", "doc_id": "n1"})).data["source"] == {"msg": "new"}
    assert run(_call(_client("a"), "delete_document", {"index": "logs", "doc_id": "n1"})).data["result"] == "deleted"
    writes = [c for c in fake.CALLS if c["method"] in {"PUT", "DELETE"}]
    assert writes and all(c["cluster"] == "cluster-a" and c["auth"] == f"ApiKey {CLUSTERS['a']['key']}" for c in writes)
    # Cluster B never saw it.
    assert not [c for c in fake.CALLS if c["cluster"] == "cluster-b"]


def test_missing_document_is_reported_as_not_found(connector):
    with pytest.raises(ToolError, match="not found"):
        run(_call(_client("a"), "get_document", {"index": "logs", "doc_id": "nope"}))


def test_wrong_key_is_rejected_without_leaking_it(connector):
    with pytest.raises(ToolError) as exc:
        run(_call(_client("a", key="key-totally-wrong"), "cluster_health"))
    assert "rejected the credentials" in str(exc.value)
    assert "key-totally-wrong" not in str(exc.value)


def test_rate_limit_is_reported_with_retry_after(connector):
    with pytest.raises(ToolError, match="Retry after 7"):
        run(_call(_client("a"), "search", {"index": "busy"}))


def test_missing_url_header_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Missing required header 'x-elasticsearch-url'"):
        run(_call(_client(None, auth=f"ApiKey {CLUSTERS['a']['key']}"), "cluster_health"))


def test_missing_authorization_header_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Missing required header 'authorization'"):
        run(_call(_client(None, url=CLUSTERS["a"]["url"]), "cluster_health"))


def test_malformed_authorization_header_gives_clear_error(connector):
    with pytest.raises(ToolError, match="ApiKey"):
        run(_call(_client("a", auth="just-a-key"), "cluster_health"))


def test_url_outside_the_allowlist_is_refused(connector):
    for bad in ["http://169.254.169.254", "http://localhost:9200", "https://evil.example.com"]:
        with pytest.raises(ToolError, match="not allowed"):
            run(_call(_client("a", url=bad), "cluster_health"))


def test_bad_index_names_are_refused(connector):
    for bad in ["../_cluster/settings", "_security", "a b", "logs/../x"]:
        with pytest.raises(ToolError, match="Invalid index"):
            run(_call(_client("a"), "search", {"index": bad}))
