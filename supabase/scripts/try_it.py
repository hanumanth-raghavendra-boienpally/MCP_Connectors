"""Call a running Supabase connector as one caller and print what comes back.

Usage (the variable lasts only for that terminal window):
  PowerShell:  $env:SUPABASE_ACCESS_TOKEN = "sbp_..."
               uv run python scripts/try_it.py
  cmd:         set SUPABASE_ACCESS_TOKEN=sbp_...        (no quotes)
               uv run python scripts/try_it.py
  bash/zsh:    SUPABASE_ACCESS_TOKEN=sbp_... uv run python scripts/try_it.py [connector-url]

The token lives only in this client process. The connector never stores it.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        sys.exit("Set the SUPABASE_ACCESS_TOKEN environment variable first.")
    headers = {"Authorization": f"Bearer {token}"}
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        print("Tools:", ", ".join(t.name for t in await client.list_tools()))
        for p in (await client.call_tool("list_projects", {})).data["projects"]:
            print(f"  project {p['id']}  {p['name']}  ({p['status']})")


asyncio.run(main())
