"""End-to-end check: do the connector's actions really happen in Supabase?

For each step, this script does something THROUGH the connector, then asks the
Supabase Management API DIRECTLY (not through the connector) whether it
happened. It also proves the safety features work against your real project:
read-only connections, project scoping, the destructive-SQL guard and secret
key filtering. It creates one table named mcp_verify_<random>, pauses so you
can see it in the Supabase dashboard, then drops it and removes its migration
record.

Needs the connector running and a Supabase personal access token. Usage:
  PowerShell:  $env:SUPABASE_ACCESS_TOKEN = "sbp_..."
  cmd:         set SUPABASE_ACCESS_TOKEN=sbp_...
  then:        uv run python scripts/verify_e2e.py [connector-url] [--no-pause]

Optional: SUPABASE_PROJECT_REF=<ref> to pick the project (needed if you have
more than one). Use a throwaway project, never production.
"""

import asyncio
import json
import os
import re
import secrets
import sys

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

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


def fenced_rows(result: dict) -> list:
    m = re.search(r"<untrusted-data-([0-9a-f]{16})>\n(.*)\n</untrusted-data-\1>", result["result"], re.S)
    return json.loads(m.group(2)) if m else []


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pause = "--no-pause" not in sys.argv and sys.stdin.isatty()
    connector_url = args[0] if args else "http://127.0.0.1:8000/mcp"
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        sys.exit("Set the SUPABASE_ACCESS_TOKEN environment variable first.")
    # SUPABASE_API_BASE only changes in tests, where Supabase is replaced by a stand-in.
    api_base = os.environ.get("SUPABASE_API_BASE", "https://api.supabase.com")

    # Preflight: is the connector running, does the token work, which project?
    health_url = connector_url.rsplit("/mcp", 1)[0] + "/health"
    try:
        up = httpx.get(health_url, timeout=5).text == "ok"
    except httpx.HTTPError:
        up = False
    if not up:
        print(f"[FAIL] The connector is not answering at {health_url}.")
        print("       Start it first (other window): uv run python -m connectors.supabase.server")
        return 1

    direct = httpx.AsyncClient(base_url=api_base, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    try:
        r = await direct.get("/v1/projects")
    except httpx.HTTPError as exc:
        print(f"[FAIL] Could not reach the Supabase API ({type(exc).__name__}).")
        await direct.aclose()
        return 1
    if r.status_code == 401:
        print("[FAIL] Supabase rejected SUPABASE_ACCESS_TOKEN (HTTP 401). Check you copied the whole token.")
        await direct.aclose()
        return 1
    real_projects = r.json() if r.status_code == 200 else []
    ref = os.environ.get("SUPABASE_PROJECT_REF")
    if not ref:
        if len(real_projects) != 1:
            names = ", ".join(f"{p.get('name')} = {p.get('id') or p.get('ref')}" for p in real_projects) or "none"
            print(f"[FAIL] You have {len(real_projects)} projects ({names}).")
            print("       Set SUPABASE_PROJECT_REF to the ref of your throwaway test project.")
            await direct.aclose()
            return 1
        ref = real_projects[0].get("id") or real_projects[0].get("ref")
    project = next((p for p in real_projects if (p.get("id") or p.get("ref")) == ref), None)
    if project is None:
        print(f"[FAIL] Project {ref!r} is not visible to this token.")
        await direct.aclose()
        return 1
    if project.get("status") not in (None, "ACTIVE_HEALTHY"):
        print(f"[FAIL] Project {project.get('name')!r} is {project.get('status')}. If it is paused, restore it "
              "in the Supabase dashboard, wait until it is healthy, then run this again.")
        await direct.aclose()
        return 1

    tag = secrets.token_hex(4)
    table, migration = f"mcp_verify_{tag}", f"create_mcp_verify_{tag}"
    print(f"Project: {project.get('name')!r} ({ref})   test table: {table}\n")

    async def direct_sql(query: str) -> tuple[int, list | dict]:
        resp = await direct.post(f"/v1/projects/{ref}/database/query", json={"query": query, "read_only": True})
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    async def direct_rows() -> list | None:
        """Rows in the test table according to Supabase itself, or None if the table does not exist."""
        status, body = await direct_sql(f"select id, note from public.{table} order by id")
        return body if status < 400 and isinstance(body, list) else None

    def connect(**extra: str) -> Client:
        headers = {"Authorization": f"Bearer {token}", **extra}
        return Client(StreamableHttpTransport(connector_url, headers=headers), timeout=60)

    created = False
    async with connect() as mcp:

        async def tool(name: str, params: dict | None = None):
            return (await mcp.call_tool(name, params or {})).data

        try:
            # 1. Account view matches Supabase's own.
            via = {p["id"] for p in (await tool("list_projects"))["projects"]}
            expect(via == {p.get("id") or p.get("ref") for p in real_projects}, "list_projects matches Supabase",
                   f"{len(via)} project(s)")
            expect((await tool("get_project", {"project_id": ref}))["status"] == "ACTIVE_HEALTHY",
                   "get_project reports the project healthy")

            # 2. Create a table with a tracked migration, confirm directly.
            await tool("apply_migration", {"project_id": ref, "name": migration,
                                           "query": f"create table public.{table} (id integer primary key, note text not null)"})
            created = True
            expect(await direct_rows() == [], "table really exists in Supabase (direct check)")
            real_mig = (await direct.get(f"/v1/projects/{ref}/database/migrations")).json()
            via_mig = [m["name"] for m in (await tool("list_migrations", {"project_id": ref}))["migrations"]]
            expect(migration in via_mig and migration in [m.get("name") for m in real_mig],
                   "migration recorded, seen by connector and by Supabase directly")

            # 3. Write rows through the connector, confirm directly.
            await tool("execute_sql", {"project_id": ref, "query":
                                       f"insert into public.{table} (id, note) values (1, 'hello {tag}'), (2, 'world {tag}')"})
            rows = await direct_rows()
            expect(rows == [{"id": 1, "note": f"hello {tag}"}, {"id": 2, "note": f"world {tag}"}],
                   "inserted rows really in Supabase (direct check)")

            # 4. Read back through the connector (fenced as untrusted data).
            res = await tool("execute_sql", {"project_id": ref, "query": f"select id, note from public.{table} order by id"})
            expect([r["note"] for r in fenced_rows(res)] == [f"hello {tag}", f"world {tag}"]
                   and "untrusted-data" in res["result"], "execute_sql returns the rows, fenced as untrusted")
            tables = {t["name"]: t for t in (await tool("list_tables", {"project_id": ref, "verbose": True}))["tables"]}
            t = tables.get(table, {})
            got_cols = [c["name"] for c in t.get("columns", [])]
            expect(t.get("primary_key") == ["id"] and got_cols == ["id", "note"],
                   "list_tables shows the table, its columns and primary key",
                   f"found={bool(t)}, columns={got_cols}, primary_key={t.get('primary_key')}")
            expect(table in (await tool("generate_typescript_types", {"project_id": ref}))["types"],
                   "generate_typescript_types includes the table")

            # 5. Safety: read-only connection refuses writes; Supabase confirms nothing changed.
            async with connect(**{"X-Supabase-Read-Only": "true"}) as ro:
                try:
                    await ro.call_tool("execute_sql", {"project_id": ref,
                                                       "query": f"insert into public.{table} values (3, 'should not exist')"})
                    refused = False
                except ToolError:
                    refused = True
                ok_read = fenced_rows((await ro.call_tool("execute_sql", {
                    "project_id": ref, "query": f"select count(*) as n from public.{table}"})).data)
            expect(refused and len(await direct_rows() or []) == 2,
                   "read-only connection refused a write; Supabase still has 2 rows (direct check)")
            expect(ok_read and int(ok_read[0]["n"]) == 2, "read-only connection can still read")

            # 6. Safety: destructive SQL is refused without explicit permission.
            try:
                await tool("execute_sql", {"project_id": ref, "query": f"drop table public.{table}"})
                guarded = False
            except ToolError:
                guarded = True
            expect(guarded and await direct_rows() is not None,
                   "DROP refused without allow_destructive; table still there (direct check)")

            # 7. Safety: a project-scoped connection works and refuses account-wide tools.
            async with connect(**{"X-Supabase-Project-Ref": ref}) as scoped:
                n = fenced_rows((await scoped.call_tool("execute_sql", {
                    "query": f"select count(*) as n from public.{table}"})).data)
                try:
                    await scoped.call_tool("list_projects", {})
                    blocked = False
                except ToolError:
                    blocked = True
            expect(n and int(n[0]["n"]) == 2 and blocked,
                   "scoped connection uses its project and blocks account tools")

            # 8. Keys: only client-safe ones come back, while secret ones do exist.
            keys = (await tool("get_publishable_keys", {"project_id": ref}))["keys"]
            all_keys = (await direct.get(f"/v1/projects/{ref}/api-keys", params={"reveal": "false"})).json()
            secret_names = {k.get("name") for k in all_keys if k.get("type") == "secret" or k.get("name") == "service_role"}
            leaked = [k for k in keys if k["name"] == "service_role" or k["type"] not in ("publishable", "legacy_anon")]
            expect(bool(keys) and not leaked, "get_publishable_keys returns only client-safe keys",
                   f"{len(keys)} returned, {len(secret_names)} secret key name(s) withheld")
            expect((await tool("get_project_url", {"project_id": ref}))["url"] == f"https://{ref}.supabase.co",
                   "get_project_url is the project's API URL")

            # 9. Other read tools work against the real project.
            expect(len((await tool("list_extensions", {"project_id": ref}))["extensions"]) > 0,
                   "list_extensions returns extensions")
            await tool("list_storage_buckets", {"project_id": ref})
            await tool("list_edge_functions", {"project_id": ref})
            await tool("get_storage_config", {"project_id": ref})
            record("PASS", "storage, edge function and storage-config tools answer")
            lints = (await tool("get_advisors", {"project_id": ref, "kind": "security"}))["lints"]
            if any(table in json.dumps(l) for l in lints):
                record("PASS", "security advisor flags the new table (no Row Level Security)")
            else:
                record("WARN", "security advisor has not flagged the new table yet",
                       f"{len(lints)} lint(s); the advisor can lag behind new tables")

            # 10. Eyes-on moment.
            print("\nLOOK NOW: open https://supabase.com/dashboard, then your project.")
            print(f"  Table Editor            -> table '{table}' with 2 rows: 'hello {tag}' and 'world {tag}'")
            print(f"  Database -> Migrations  -> '{migration}'")
            print("Take screenshots.")
            if pause:
                input("Press Enter to continue (the table and its migration record are removed next)... ")

            # 11. Drop through the connector (explicitly allowed), confirm really gone.
            await tool("execute_sql", {"project_id": ref, "query": f"drop table public.{table}", "allow_destructive": True})
            if expect(await direct_rows() is None, "table is really gone from Supabase (direct check)"):
                created = False
        except ToolError as exc:
            record("FAIL", "connector call raised an error", str(exc))
        except httpx.HTTPError as exc:
            record("FAIL", "direct request to Supabase failed", type(exc).__name__)
        finally:
            try:
                if created:
                    await direct.post(f"/v1/projects/{ref}/database/query",
                                      json={"query": f"drop table if exists public.{table}"})
                    record("INFO", "cleanup: test table dropped directly", table)
                resp = await direct.post(f"/v1/projects/{ref}/database/query", json={
                    "query": f"delete from supabase_migrations.schema_migrations where name = '{migration}'"})
                record("INFO" if resp.status_code < 400 else "WARN",
                       "cleanup: migration record removed" if resp.status_code < 400
                       else "could not remove the test migration record; it is harmless", migration)
            except httpx.HTTPError as exc:
                record("WARN", "cleanup request failed; check the dashboard for leftovers", type(exc).__name__)
            await direct.aclose()

    return summary()


sys.exit(asyncio.run(main()))
