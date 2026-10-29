"""Call a running Postman connector as one caller and print what comes back.

Usage (the variable lasts only for that terminal window):
  PowerShell:  $env:POSTMAN_API_KEY = "PMAK-..."
               uv run python scripts/try_it.py
  cmd:         set POSTMAN_API_KEY=PMAK-...        (no quotes)
               uv run python scripts/try_it.py
  bash/zsh:    POSTMAN_API_KEY=PMAK-... uv run python scripts/try_it.py [connector-url]

The key lives only in this client process. The connector never stores it.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    key = os.environ.get("POSTMAN_API_KEY")
    if not key:
        sys.exit("Set the POSTMAN_API_KEY environment variable first.")
    headers = {"X-Api-Key": key}
    if os.environ.get("POSTMAN_REGION", "").lower() == "eu":
        headers["X-Postman-Region"] = "eu"
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        print("Tools:", ", ".join(t.name for t in await client.list_tools()))
        me = (await client.call_tool("get_me", {})).data
        print(f"Signed in as: {me['username']}")
        for w in (await client.call_tool("list_workspaces", {})).data["workspaces"]:
            print(f"  workspace {w['id']}  {w['name']}  ({w['type']})")


asyncio.run(main())
