"""Call a running Slack connector as one caller and print what comes back.

Usage (the variable lasts only for that terminal window):
  PowerShell:  $env:SLACK_TOKEN = "xoxb-..."
               uv run python scripts/try_it.py
  cmd:         set SLACK_TOKEN=xoxb-...        (no quotes)
               uv run python scripts/try_it.py
  bash/zsh:    SLACK_TOKEN=xoxb-... uv run python scripts/try_it.py [url]

The token lives only in this client process. The server never stores it.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    token = os.environ.get("SLACK_TOKEN")
    if not token:
        sys.exit("Set the SLACK_TOKEN environment variable first.")
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        print("Tools:", ", ".join(t.name for t in await client.list_tools()))
        result = await client.call_tool("list_channels", {})
        for channel in result.data["channels"]:
            print(f"  {channel['id']}  #{channel['name']}")


asyncio.run(main())
