"""End-to-end check: do the connector's actions really happen in Slack?

For each step, this script does something THROUGH the connector, then asks
Slack DIRECTLY (not through the connector) whether it happened. It posts a
test message in one channel, reacts to it, replies in a thread, then deletes
its own test messages at the end.

Needs the connector running and a Slack bot token. Usage:
  PowerShell:  $env:SLACK_TOKEN = "xoxb-..."; $env:SLACK_CHANNEL_ID = "C0123456789"
  cmd:         set SLACK_TOKEN=xoxb-...   and   set SLACK_CHANNEL_ID=C0123456789
  then:        uv run python scripts/verify_e2e.py [connector-url] [--no-pause]

SLACK_CHANNEL_ID is the ID (not the name) of a channel the bot is a member of.
Run scripts/try_it.py to list channel IDs. Use a throwaway channel.
Optional bot scope: reactions:read (without it the reaction check is skipped).
"""

import asyncio
import os
import secrets
import sys

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

# Only changes in tests, where Slack is replaced by a stand-in.
SLACK_API = os.environ.get("SLACK_API_BASE", "https://slack.com/api")
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
    token = os.environ.get("SLACK_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel:
        sys.exit("Set the SLACK_TOKEN and SLACK_CHANNEL_ID environment variables first.")

    marker = f"MCP verify {secrets.token_hex(4)}"
    reply_text = f"{marker} (thread reply)"
    print(f"Test message text: {marker!r}\n")

    direct = httpx.AsyncClient(
        base_url=SLACK_API, headers={"Authorization": f"Bearer {token}"}, timeout=20
    )

    async def slack(method: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        """Ask Slack directly. Returns Slack's JSON (check data['ok'])."""
        resp = await (direct.post(f"/{method}", json=body) if body is not None
                      else direct.get(f"/{method}", params=params))
        return resp.json()

    posted: list[str] = []  # timestamps of messages this script created

    async with Client(
        StreamableHttpTransport(connector_url, headers={"Authorization": f"Bearer {token}"}),
        timeout=30,
    ) as mcp:

        async def tool(name: str, params: dict):
            return (await mcp.call_tool(name, params)).data

        try:
            # 1. The channel is visible through the connector.
            chans = (await tool("list_channels", {"limit": 200}))["channels"]
            if channel in [c["id"] for c in chans]:
                record("PASS", "list_channels shows the test channel")
            else:
                record("WARN", "test channel not in the first 200 listed", "continuing anyway")

            # 2. Post through the connector, confirm in Slack directly.
            ts = (await tool("post_message", {"channel_id": channel, "text": marker}))["ts"]
            posted.append(ts)
            record("PASS", "post_message returned a timestamp", ts)
            real = await slack("conversations.history",
                               params={"channel": channel, "latest": ts, "inclusive": "true", "limit": 1})
            msgs = real.get("messages", [])
            expect(bool(msgs) and msgs[0].get("text") == marker and msgs[0].get("ts") == ts,
                   "message really exists in Slack (direct check)",
                   "" if real.get("ok") else f"Slack said: {real.get('error')}")

            # 3. Read it back through the connector.
            hist = (await tool("get_channel_history", {"channel_id": channel, "limit": 10}))["messages"]
            expect(any(m["text"] == marker for m in hist), "get_channel_history returns the message")

            # 4. React through the connector, confirm in Slack directly.
            await tool("add_reaction", {"channel_id": channel, "timestamp": ts, "emoji": "eyes"})
            rx = await slack("reactions.get", params={"channel": channel, "timestamp": ts})
            if rx.get("ok"):
                names = [r["name"] for r in rx["message"].get("reactions", [])]
                expect("eyes" in names, "reaction really on the message in Slack (direct check)")
            else:
                record("SKIP", "reaction direct check", f"Slack said {rx.get('error')}; confirm by eye below")

            # 5. Thread reply through the connector, confirm in Slack directly.
            reply_ts = (await tool("post_message", {"channel_id": channel, "text": reply_text, "thread_ts": ts}))["ts"]
            posted.append(reply_ts)
            replies = (await tool("get_thread_replies", {"channel_id": channel, "thread_ts": ts}))["messages"]
            expect({marker, reply_text} <= {m["text"] for m in replies}, "get_thread_replies shows parent and reply")
            real_rep = await slack("conversations.replies", params={"channel": channel, "ts": ts})
            expect(any(m.get("text") == reply_text for m in real_rep.get("messages", [])),
                   "thread reply really exists in Slack (direct check)")

            # 6. Read-only listings.
            try:
                users = (await tool("list_users", {"limit": 50}))["users"]
                expect(len(users) >= 1, "list_users returns people", f"{len(users)} returned")
            except ToolError as exc:
                record("WARN", "list_users failed (bot may lack the users:read scope)", str(exc))

            # 7. Eyes-on moment.
            print("\nLOOK NOW: open the test channel in the Slack app.")
            print(f"You should see: '{marker}' with an eyes emoji, and a thread reply under it.")
            print("Take a screenshot.")
            if pause:
                input("Press Enter to continue (the test messages are deleted next)... ")
        except ToolError as exc:
            record("FAIL", "connector call raised an error", str(exc))
        finally:
            # Clean up only the messages this script posted (reply first).
            for ts_ in reversed(posted):
                res = await slack("chat.delete", body={"channel": channel, "ts": ts_})
                if res.get("ok"):
                    record("INFO", "cleanup: test message deleted", ts_)
                else:
                    record("WARN", "could not delete test message; delete it by hand",
                           f"{ts_}: {res.get('error')}")
            await direct.aclose()

    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\nSummary: {len([r for r in RESULTS if r[0] == 'PASS'])} passed, {len(failed)} failed, "
          f"{len([r for r in RESULTS if r[0] == 'SKIP'])} skipped, "
          f"{len([r for r in RESULTS if r[0] == 'WARN'])} warnings")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
