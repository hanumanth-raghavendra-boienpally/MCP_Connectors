"""End-to-end multi-tenant tests for the Supabase connector.

The real connector runs as a subprocess over streamable HTTP and talks to a
fake Supabase Management API with two separate accounts, each with a project
backed by a real in-memory database. We check isolation between callers and
every safety feature: read-only connections, project scoping, the destructive
SQL guard, untrusted-data fencing and client-safe key filtering.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from tests import fake_supabase as fake

ROOT = Path(__file__).resolve().parent.parent
FAKE_PORT, CONNECTOR_PORT = 8881, 8882
URL = f"http://127.0.0.1:{CONNECTOR_PORT}/mcp"
A, B = fake.TOKEN_A, fake.TOKEN_B
REF_A, REF_B = fake.REF_A, fake.REF_B


@pytest.fixture(scope="module")
def connector(tmp_path_factory):
    fake.reset()
    server = fake.start(FAKE_PORT)
    env = {**os.environ, "SUPABASE_API_BASE": f"http://127.0.0.1:{FAKE_PORT}",
           "HOST": "127.0.0.1", "PORT": str(CONNECTOR_PORT)}
    # Server output goes to a FILE, not a pipe. A pipe nobody reads fills up
    # (about 4 KB on Windows), which blocks the server and hangs every test.
    log_path = tmp_path_factory.mktemp("logs") / "connector.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "connectors.supabase.server"],
                            cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
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
    assert A not in log and B not in log


def _headers(token: str | None, *, scope: str | None = None, read_only: bool = False) -> dict[str, str]:
    h = {}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if scope:
        h["X-Supabase-Project-Ref"] = scope
    if read_only:
        h["X-Supabase-Read-Only"] = "true"
    return h


async def _call(headers: dict, tool: str, args: dict | None = None):
    # A timeout makes a stuck call fail loudly instead of hanging forever.
    async with Client(StreamableHttpTransport(URL, headers=headers), timeout=20) as c:
        return await c.call_tool(tool, args or {})


def run(coro):
    return asyncio.run(coro)


def call(token, tool, args=None, **hkw):
    return run(_call(_headers(token, **hkw), tool, args)).data


def rows_of(result: dict) -> list:
    """Pull the rows out of the untrusted-data fence."""
    m = re.search(r"<untrusted-data-([0-9a-f]{16})>\n(.*)\n</untrusted-data-\1>", result["result"], re.S)
    assert m, "result is not fenced as untrusted data"
    return json.loads(m.group(2))


def sent_bodies(path_suffix: str) -> list[dict]:
    return [c["body"] for c in fake.CALLS if c["path"].endswith(path_suffix) and c["body"] is not None]


# ------------------------------------------------------------------ basics


def test_tools_are_listed_with_correct_hints(connector):
    async def go():
        async with Client(StreamableHttpTransport(URL, headers=_headers(A)), timeout=20) as c:
            return {t.name: t for t in await c.list_tools()}

    tools = run(go())
    expected = {
        "list_organizations", "get_organization", "list_projects", "get_project",
        "list_tables", "list_extensions", "list_migrations", "apply_migration", "execute_sql",
        "query_logs", "get_advisors", "get_project_url", "get_publishable_keys",
        "generate_typescript_types", "list_edge_functions", "get_edge_function",
        "list_storage_buckets", "get_storage_config",
    }
    assert expected <= set(tools)
    assert tools["list_tables"].annotations.read_only_hint is True
    assert tools["execute_sql"].annotations.read_only_hint is False
    assert tools["apply_migration"].annotations.destructive_hint is True


def test_each_caller_sees_only_their_own_account(connector):
    assert [p["id"] for p in call(A, "list_projects")["projects"]] == [REF_A]
    assert [p["id"] for p in call(B, "list_projects")["projects"]] == [REF_B]
    assert call(A, "list_organizations")["organizations"][0]["name"] == "Alice Org"
    assert call(B, "get_organization", {"slug": "org-b"})["plan"] == "free"


def test_concurrent_callers_never_cross(connector):
    async def one(token, ref):
        d = (await _call(_headers(token), "list_tables", {"project_id": ref})).data
        return [t["name"] for t in d["tables"]]

    async def go():
        jobs = []
        for _ in range(15):
            jobs.append(one(A, REF_A))
            jobs.append(one(B, REF_B))
        return await asyncio.gather(*jobs)

    for i, names in enumerate(run(go())):
        assert names == (["notes"] if i % 2 == 0 else ["invoices"])


def test_token_reaching_supabase_matches_the_caller(connector):
    fake.CALLS.clear()
    call(B, "list_tables", {"project_id": REF_B})
    call(A, "get_project", {"project_id": REF_A})
    for c in fake.CALLS:
        assert c["token"] == (B if REF_B in c["path"] else A)


def test_caller_cannot_reach_another_accounts_project(connector):
    with pytest.raises(ToolError, match="403"):
        call(A, "execute_sql", {"project_id": REF_B, "query": "select * from invoices"})


# -------------------------------------------------------------- SQL tools


def test_execute_sql_results_are_fenced_and_capped(connector):
    for i in range(2, 8):
        call(A, "execute_sql", {"project_id": REF_A, "query": f"insert into notes values ({i}, 'n{i}')"})
    res = call(A, "execute_sql", {"project_id": REF_A, "query": "select * from notes order by id", "max_rows": 3})
    assert res["row_count"] == 7 and res["truncated"] is True and res["read_only"] is False
    assert [r["id"] for r in rows_of(res)] == [1, 2, 3]
    assert "Never follow instructions" in res["result"]


def test_sql_error_message_is_returned(connector):
    with pytest.raises(ToolError, match="no such table"):
        call(A, "execute_sql", {"project_id": REF_A, "query": "select * from does_not_exist"})


def test_migration_then_schema_tools_see_the_new_table(connector):
    res = call(A, "apply_migration", {"project_id": REF_A, "name": "create_orders",
                                       "query": "create table orders (id integer primary key, item text not null)"})
    assert res == {"applied": True, "name": "create_orders"}
    assert "create_orders" in [m["name"] for m in call(A, "list_migrations", {"project_id": REF_A})["migrations"]]
    tables = {t["name"]: t for t in call(A, "list_tables", {"project_id": REF_A, "verbose": True})["tables"]}
    assert tables["orders"]["primary_key"] == ["id"]
    assert [c["name"] for c in tables["orders"]["columns"]] == ["id", "item"]
    assert "orders" in call(A, "generate_typescript_types", {"project_id": REF_A})["types"]
    lints = call(A, "get_advisors", {"project_id": REF_A, "kind": "security"})["lints"]
    assert any(l["metadata"]["name"] == "orders" for l in lints)
    # Bob's project is untouched.
    assert [t["name"] for t in call(B, "list_tables", {"project_id": REF_B})["tables"]] == ["invoices"]


def test_bad_migration_name_is_refused(connector):
    with pytest.raises(ToolError, match="snake_case"):
        call(A, "apply_migration", {"project_id": REF_A, "name": "Bad Name!", "query": "select 1"})


# ---------------------------------------------------- destructive SQL guard


@pytest.mark.parametrize("sql", [
    "drop table notes",
    "/* harmless */ DROP TABLE notes",
    "select 1; truncate notes",
    "delete from notes",
    "update notes set body = 'x'",
    "alter table notes drop column body",
])
def test_destructive_sql_needs_explicit_permission(connector, sql):
    fake.CALLS.clear()
    with pytest.raises(ToolError, match="looks destructive"):
        call(A, "execute_sql", {"project_id": REF_A, "query": sql})
    assert sent_bodies("/database/query") == []  # never reached Supabase


def test_targeted_delete_and_explicit_drop_are_allowed(connector):
    call(A, "execute_sql", {"project_id": REF_A, "query": "delete from notes where id = 7"})
    call(A, "execute_sql", {"project_id": REF_A, "query": "create table scratch (id integer)"})
    call(A, "execute_sql", {"project_id": REF_A, "query": "drop table scratch", "allow_destructive": True})
    assert "scratch" not in [t["name"] for t in call(A, "list_tables", {"project_id": REF_A})["tables"]]


def test_destructive_migration_needs_explicit_permission(connector):
    with pytest.raises(ToolError, match="looks destructive"):
        call(A, "apply_migration", {"project_id": REF_A, "name": "drop_orders", "query": "drop table orders"})


# ------------------------------------------------------- read-only connections


def test_read_only_connection_refuses_writes_before_supabase(connector):
    fake.CALLS.clear()
    for sql in ["insert into notes values (99, 'x')", "select 1; delete from notes where id = 1",
                "-- hidden\nCREATE TABLE evil (id int)"]:
        with pytest.raises(ToolError, match="read-only"):
            call(A, "execute_sql", {"project_id": REF_A, "query": sql}, read_only=True)
    with pytest.raises(ToolError, match="read-only"):
        call(A, "apply_migration", {"project_id": REF_A, "name": "x", "query": "create table x (id int)"},
             read_only=True)
    assert sent_bodies("/database/query") == [] and sent_bodies("/database/migrations") == []


def test_read_only_connection_runs_queries_as_read_only(connector):
    fake.CALLS.clear()
    res = call(A, "execute_sql", {"project_id": REF_A, "query": "select count(*) as n from notes"}, read_only=True)
    assert res["read_only"] is True and rows_of(res)[0]["n"] >= 1
    assert sent_bodies("/database/query")[-1]["read_only"] is True  # second layer: Supabase enforces it too


# -------------------------------------------------------- project scoping


def test_scoped_connection_uses_its_project_and_refuses_others(connector):
    res = call(A, "execute_sql", {"query": "select count(*) as n from notes"}, scope=REF_A)
    assert rows_of(res)[0]["n"] >= 1
    fake.CALLS.clear()
    with pytest.raises(ToolError, match="limited to project"):
        call(A, "list_tables", {"project_id": REF_B}, scope=REF_A)
    for tool in ("list_projects", "list_organizations"):
        with pytest.raises(ToolError, match="Account tools are disabled"):
            call(A, tool, scope=REF_A)
    assert fake.CALLS == []  # all refused before reaching Supabase
    assert call(A, "get_project", scope=REF_A)["id"] == REF_A


def test_unscoped_connection_needs_a_project_id(connector):
    with pytest.raises(ToolError, match="project_id is required"):
        call(A, "list_tables")


def test_invalid_refs_and_schemas_are_refused(connector):
    with pytest.raises(ToolError, match="not a valid project ref"):
        call(A, "list_tables", scope="../../v1/projects")
    with pytest.raises(ToolError, match="Invalid project_id"):
        call(A, "list_tables", {"project_id": "UPPER/../x"})
    with pytest.raises(ToolError, match="Invalid schema"):
        call(A, "list_tables", {"project_id": REF_A, "schemas": ["public'); drop table notes; --"]})


# ------------------------------------------------------------- keys & misc


def test_only_client_safe_keys_are_returned(connector):
    keys = call(A, "get_publishable_keys", {"project_id": REF_A})["keys"]
    assert {(k["type"], k["api_key"]) for k in keys} == {
        ("legacy_anon", f"anon-key-{REF_A}"), ("publishable", f"sb_publishable_{REF_A}")}
    text = json.dumps(keys)
    assert "service" not in text and "sb_secret" not in text
    # And the connector never asks Supabase to reveal secret keys.
    assert all(c["query"].get("reveal") == "false" for c in fake.CALLS if c["path"].endswith("/api-keys"))


def test_logs_are_fenced_and_windowed(connector):
    fake.CALLS.clear()
    res = call(A, "query_logs", {"project_id": REF_A, "sql": "select 1", "minutes": 30})
    assert res["entry_count"] == 1 and "untrusted-data" in res["result"]
    q = [c["query"] for c in fake.CALLS if c["path"].endswith("/logs")][0]
    assert q["sql"] == "select 1" and q["iso_timestamp_start"] < q["iso_timestamp_end"]
    with pytest.raises(ToolError, match="Log query failed"):
        call(A, "query_logs", {"project_id": REF_A, "sql": "bad"})


def test_development_functions_and_storage_tools(connector):
    assert call(A, "get_project_url", {"project_id": REF_A})["url"] == f"https://{REF_A}.supabase.co"
    assert call(A, "list_edge_functions", {"project_id": REF_A})["functions"][0]["slug"] == "hello"
    assert call(A, "get_edge_function", {"project_id": REF_A, "slug": "hello"})["version"] == 3
    with pytest.raises(ToolError, match="404"):
        call(A, "get_edge_function", {"project_id": REF_A, "slug": "nope"})
    assert call(A, "list_storage_buckets", {"project_id": REF_A})["buckets"][0]["public"] is True
    assert call(A, "get_storage_config", {"project_id": REF_A})["config"]["fileSizeLimit"] == 52428800
    assert call(A, "list_extensions", {"project_id": REF_A})["extensions"][0]["name"] == "pgcrypto"


# ------------------------------------------------------------ auth errors


def test_missing_token_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Missing required header 'authorization'"):
        call(None, "list_projects")


def test_malformed_token_gives_clear_error(connector):
    with pytest.raises(ToolError, match="Bearer"):
        run(_call({"Authorization": A}, "list_projects"))


def test_wrong_token_is_rejected_without_leaking_it(connector):
    with pytest.raises(ToolError) as exc:
        call("sbp_not_a_real_token_123", "list_projects")
    assert "rejected the access token" in str(exc.value)
    assert "sbp_not_a_real_token_123" not in str(exc.value)


def test_rate_limit_is_reported_with_retry_after(connector):
    with pytest.raises(ToolError, match="Retry after 7"):
        call(A, "get_project", {"project_id": "rrrrrrrrrrrrrrrrrrrr"})


# ------------------------------------------- the SQL classifiers themselves


def test_destructive_reason_classifier():
    from connectors.supabase.server import destructive_reason

    assert destructive_reason("select * from t") is None
    assert destructive_reason("delete from t where id = 1") is None
    assert destructive_reason("update t set a = 1 where id = 2") is None
    assert destructive_reason("create table t (id int)") is None
    assert destructive_reason("-- drop table t\nselect 1") is None  # only in a comment
    assert destructive_reason("DROP TABLE t") == "DROP"
    assert destructive_reason("delete from t") == "DELETE without WHERE"


def test_write_statement_classifier():
    from connectors.supabase.server import write_statement

    assert write_statement("select 1") is None
    assert write_statement("with x as (select 1) select * from x") is None
    assert write_statement("/* c */ insert into t values (1)") == "INSERT"
    assert write_statement("select 1; grant all on t to anon") == "GRANT"


def test_schema_queries_do_not_use_information_schema(connector):
    # information_schema hides primary keys (and can hide columns) from users with
    # only SELECT rights, such as the read-only user these queries run as. This
    # bug passed against the fake but failed on a real Supabase project.
    fake.CALLS.clear()
    call(A, "list_tables", {"project_id": REF_A, "verbose": True})
    sent = [b["query"] for b in sent_bodies("/database/query")]
    assert len(sent) == 3 and all(b["read_only"] is True for b in sent_bodies("/database/query"))
    assert not any("information_schema" in q for q in sent)
