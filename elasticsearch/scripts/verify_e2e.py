"""End-to-end check: do the connector's actions really happen in Elasticsearch?

For each step, this script does something THROUGH the connector, then looks at
Elasticsearch DIRECTLY (not through the connector) to confirm it happened.
It uses a throwaway index named mcp-verify-<random> and deletes it at the end.

Needs the connector and an Elasticsearch running. Usage:
  PowerShell:  $env:ES_URL = "http://localhost:9200"; $env:ES_API_KEY = "anything"
  cmd:         set ES_URL=http://localhost:9200   and   set ES_API_KEY=anything
  then:        uv run python scripts/verify_e2e.py [connector-url] [--no-pause]

With the local Docker Elasticsearch (security off) any ES_API_KEY works.
"""

import asyncio
import os
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


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pause = "--no-pause" not in sys.argv and sys.stdin.isatty()
    connector_url = args[0] if args else "http://127.0.0.1:8000/mcp"
    es_url = (os.environ.get("ES_URL") or "").rstrip("/")
    key = os.environ.get("ES_API_KEY")
    if not es_url or not key:
        sys.exit("Set the ES_URL and ES_API_KEY environment variables first.")

    auth = {"Authorization": f"ApiKey {key}"}
    index = f"mcp-verify-{secrets.token_hex(4)}"  # only this index is ever touched
    doc_id = "check-1"
    marker = f"verify-{secrets.token_hex(6)}"
    print(f"Throwaway index: {index}   marker: {marker}\n")

    headers = {"X-Elasticsearch-URL": es_url, **auth}
    direct = httpx.AsyncClient(base_url=es_url, headers=auth, timeout=20)

    # Preflight: is everything we need actually running? Say so plainly if not.
    health_url = connector_url.rsplit("/mcp", 1)[0] + "/health"
    try:
        ok = httpx.get(health_url, timeout=5).text == "ok"
    except httpx.HTTPError:
        ok = False
    if not ok:
        print(f"[FAIL] The connector is not answering at {health_url}.")
        print("       Start it first (window 2): uv run python -m connectors.elasticsearch.server")
        await direct.aclose()
        return 1
    try:
        pre = await direct.get("/_cluster/health", timeout=10)
    except httpx.HTTPError as exc:
        print(f"[FAIL] Elasticsearch is not answering at {es_url} ({type(exc).__name__}).")
        print("       Is the Docker container running and fully started (about 30-60 seconds)?")
        print(f"       Open {es_url} in your browser: you should see JSON containing 'cluster_name'.")
        print("       If Docker Desktop was just started, wait a minute and run this again.")
        await direct.aclose()
        return 1
    if pre.status_code != 200:
        print(f"[FAIL] Elasticsearch answered HTTP {pre.status_code} at {es_url}; check ES_URL and ES_API_KEY.")
        await direct.aclose()
        return 1

    async with Client(StreamableHttpTransport(connector_url, headers=headers), timeout=30) as mcp:

        async def tool(name: str, params: dict):
            return (await mcp.call_tool(name, params)).data

        try:
            # 1. Cluster health, compared with what Elasticsearch itself says.
            via = await tool("cluster_health", {})
            real = (await direct.get("/_cluster/health")).json()
            expect(via["cluster_name"] == real["cluster_name"], "cluster_health matches Elasticsearch",
                   f"cluster {real['cluster_name']}, status {real['status']}")

            # 2. Write through the connector, confirm in Elasticsearch directly.
            res = await tool("index_document", {"index": index, "doc_id": doc_id,
                                                "document": {"msg": marker, "level": "info"}})
            expect(res["result"] == "created", "index_document reports created")
            r = await direct.get(f"/{index}/_doc/{doc_id}")
            body = r.json() if r.status_code == 200 else {}
            expect(r.status_code == 200 and body.get("_source", {}).get("msg") == marker,
                   "document really exists in Elasticsearch (direct check)")

            # 3. Read paths through the connector.
            await direct.post(f"/{index}/_refresh")  # make it searchable now
            hits = (await tool("search", {"index": index, "query": {"match": {"msg": marker}}}))
            expect(hits["total"] == 1 and hits["hits"][0]["id"] == doc_id, "search finds the document")
            expect((await tool("count", {"index": index}))["count"] == 1, "count is 1")
            expect(doc_id == (await tool("get_document", {"index": index, "doc_id": doc_id}))["id"],
                   "get_document returns it")
            mapping = await tool("get_mapping", {"index": index})
            expect("msg" in mapping[index]["mappings"]["properties"], "get_mapping lists the fields")
            listed = [i["index"] for i in (await tool("list_indices", {"pattern": "mcp-verify-*"}))["indices"]]
            expect(index in listed, "list_indices shows the index")
            shards = await tool("get_shards", {"pattern": index})
            via = sorted((s["shard"], s["primary"], s["state"]) for s in shards["shards"])
            raw = (await direct.get(f"/_cat/shards/{index}", params={"format": "json", "h": "shard,prirep,state"})).json()
            real = sorted((r["shard"], r["prirep"] == "p", r["state"]) for r in raw)
            expect(bool(via) and via == real, "get_shards matches Elasticsearch's own shard list (direct check)",
                   ", ".join(f"{state}: {n}" for state, n in sorted(shards["by_state"].items())))
            try:
                rows = (await tool("esql_query", {"query": f"FROM {index} | LIMIT 5"}))["rows"]
                expect(any(marker in str(row) for row in rows), "esql_query returns the document")
            except ToolError as exc:
                record("SKIP", "esql_query", f"needs Elasticsearch 8.11+ ({exc})")

            # 4. Eyes-on moment: look at it yourself and screenshot it.
            print(f"\nLOOK NOW: open  {es_url}/{index}/_doc/{doc_id}  in your browser.")
            print("You should see the document with the marker above. Take a screenshot.")
            if pause:
                input("Press Enter to continue (the document is deleted next)... ")

            # 5. Delete through the connector, confirm it is really gone.
            res = await tool("delete_document", {"index": index, "doc_id": doc_id})
            expect(res["result"] == "deleted", "delete_document reports deleted")
            r = await direct.get(f"/{index}/_doc/{doc_id}")
            expect(r.status_code == 404, "document is really gone from Elasticsearch (direct check)")
        except ToolError as exc:
            record("FAIL", "connector call raised an error", str(exc))
        except httpx.HTTPError as exc:
            record("FAIL", "direct request to Elasticsearch failed", type(exc).__name__)
        finally:
            # Clean up: delete only the throwaway index created above.
            if index.startswith("mcp-verify-"):
                try:
                    r = await direct.delete(f"/{index}")
                    record("INFO", "cleanup: throwaway index deleted", f"HTTP {r.status_code}")
                except httpx.HTTPError as exc:
                    record("WARN", f"could not delete throwaway index {index}; delete it by hand",
                           type(exc).__name__)
            await direct.aclose()

    failed = [r for r in RESULTS if r[0] == "FAIL"]
    passed = [r for r in RESULTS if r[0] == "PASS"]
    print(f"\nSummary: {len(passed)} passed, {len(failed)} failed, "
          f"{len([r for r in RESULTS if r[0] == 'SKIP'])} skipped, "
          f"{len([r for r in RESULTS if r[0] == 'WARN'])} warnings")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
