"""Call a running Elasticsearch connector as one caller and print what comes back.

Usage (variables last only for that terminal window):
  PowerShell:  $env:ES_URL = "http://127.0.0.1:9201"
               $env:ES_API_KEY = "anything-if-security-is-off"
               uv run python scripts/try_it.py
  cmd:         set ES_URL=http://127.0.0.1:9201       (no quotes)
               set ES_API_KEY=anything
               uv run python scripts/try_it.py
  bash/zsh:    ES_URL=... ES_API_KEY=... uv run python scripts/try_it.py [connector-url]

The cluster URL and key live only in this client process. The connector never
stores them.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    es_url, key = os.environ.get("ES_URL"), os.environ.get("ES_API_KEY")
    if not es_url or not key:
        sys.exit("Set the ES_URL and ES_API_KEY environment variables first.")
    headers = {"X-Elasticsearch-URL": es_url, "Authorization": f"ApiKey {key}"}
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        print("Tools:", ", ".join(t.name for t in await client.list_tools()))
        print("Health:", (await client.call_tool("cluster_health", {})).data)
        indices = (await client.call_tool("list_indices", {})).data["indices"]
        for i in indices:
            print(f"  {i['index']}  docs={i['docs_count']}  size={i['store_size']}")


asyncio.run(main())
