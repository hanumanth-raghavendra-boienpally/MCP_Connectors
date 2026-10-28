import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

H = {"X-Elasticsearch-URL": "http://localhost:9200", "Authorization": "ApiKey anything"}

async def main():
    async with Client(StreamableHttpTransport("http://127.0.0.1:8000/mcp", headers=H)) as c:
        async def call(tool, args):
            print(tool, "->", (await c.call_tool(tool, args)).data)
        await call("index_document", {"index": "demo", "doc_id": "1", "document": {"level": "error", "msg": "hello world"}})
        await call("get_document", {"index": "demo", "doc_id": "1"})
        await asyncio.sleep(2)  # Elasticsearch search needs about a second to see new documents
        await call("search", {"index": "demo", "query": {"match": {"msg": "hello"}}})
        await call("count", {"index": "demo"})
        await call("esql_query", {"query": "FROM demo | LIMIT 5"})
        await call("get_mapping", {"index": "demo"})
        await call("list_indices", {})
        await call("delete_document", {"index": "demo", "doc_id": "1"})

asyncio.run(main())