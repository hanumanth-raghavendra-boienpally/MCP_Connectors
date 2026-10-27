"""Slack MCP connector: streamable HTTP, stateless, multi-tenant.

Every caller sends their own Slack token as 'Authorization: Bearer <token>'.
The server holds no credentials and no per-user state.
"""

import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from connector_core.auth import get_bearer_token
from connector_core.http import make_client

# Operator-level setting (not caller-controlled), overridden in tests only.
SLACK_API_BASE = os.environ.get("SLACK_API_BASE", "https://slack.com/api")

mcp = FastMCP(
    "slack",
    instructions=(
        "Slack workspace access. Send your own Slack token as "
        "'Authorization: Bearer <token>' on every request."
    ),
)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _slack(
    method: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one Slack Web API method as the current caller."""
    token = get_bearer_token()
    async with make_client(SLACK_API_BASE, {"Authorization": f"Bearer {token}"}) as client:
        try:
            if body is not None:
                resp = await client.post(f"/{method}", json=body)
            else:
                resp = await client.get(f"/{method}", params=params)
        except httpx.TimeoutException:
            raise ToolError("Slack API timed out.") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not reach the Slack API ({type(exc).__name__}).") from None

    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "unknown")
        raise ToolError(f"Slack rate limit reached. Retry after {retry} seconds.")
    if resp.status_code >= 400:
        raise ToolError(f"Slack API returned HTTP {resp.status_code}.")

    data = resp.json()
    if not data.get("ok"):
        raise ToolError(f"Slack error: {data.get('error', 'unknown_error')}")
    return data


@mcp.tool(annotations=READ_ONLY)
async def list_channels(
    limit: Annotated[int, Field(ge=1, le=200, description="Max channels to return")] = 100,
    cursor: Annotated[str | None, Field(description="Pagination cursor from a previous call")] = None,
    include_private: Annotated[bool, Field(description="Also list private channels the token can see")] = False,
) -> dict[str, Any]:
    """List channels in the workspace the caller's token can see."""
    types = "public_channel,private_channel" if include_private else "public_channel"
    params: dict[str, Any] = {"limit": limit, "types": types, "exclude_archived": "true"}
    if cursor:
        params["cursor"] = cursor
    data = await _slack("conversations.list", params=params)
    return {
        "channels": [
            {
                "id": c["id"],
                "name": c.get("name"),
                "is_private": c.get("is_private", False),
                "num_members": c.get("num_members"),
                "topic": (c.get("topic") or {}).get("value"),
            }
            for c in data.get("channels", [])
        ],
        "next_cursor": (data.get("response_metadata") or {}).get("next_cursor") or None,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_channel_history(
    channel_id: Annotated[str, Field(description="Channel ID, e.g. C0123456789")],
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
    cursor: Annotated[str | None, Field(description="Pagination cursor")] = None,
) -> dict[str, Any]:
    """Get recent messages from a channel, newest first."""
    params: dict[str, Any] = {"channel": channel_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = await _slack("conversations.history", params=params)
    return {
        "messages": [
            {
                "ts": m.get("ts"),
                "user": m.get("user"),
                "text": m.get("text"),
                "thread_ts": m.get("thread_ts"),
                "reply_count": m.get("reply_count", 0),
            }
            for m in data.get("messages", [])
        ],
        "next_cursor": (data.get("response_metadata") or {}).get("next_cursor") or None,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_thread_replies(
    channel_id: Annotated[str, Field(description="Channel ID")],
    thread_ts: Annotated[str, Field(description="Timestamp of the thread's parent message")],
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Get the replies in a message thread."""
    data = await _slack(
        "conversations.replies",
        params={"channel": channel_id, "ts": thread_ts, "limit": limit},
    )
    return {
        "messages": [
            {"ts": m.get("ts"), "user": m.get("user"), "text": m.get("text")}
            for m in data.get("messages", [])
        ]
    }


@mcp.tool(annotations=READ_ONLY)
async def list_users(
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Field(description="Pagination cursor")] = None,
) -> dict[str, Any]:
    """List workspace members (id, name, display name, bot flag)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = await _slack("users.list", params=params)
    return {
        "users": [
            {
                "id": u["id"],
                "name": u.get("name"),
                "real_name": u.get("real_name"),
                "is_bot": u.get("is_bot", False),
                "deleted": u.get("deleted", False),
            }
            for u in data.get("members", [])
        ],
        "next_cursor": (data.get("response_metadata") or {}).get("next_cursor") or None,
    }


@mcp.tool(annotations=WRITE)
async def post_message(
    channel_id: Annotated[str, Field(description="Channel ID to post in")],
    text: Annotated[str, Field(min_length=1, description="Message text (Slack mrkdwn supported)")],
    thread_ts: Annotated[str | None, Field(description="Reply inside this thread if set")] = None,
) -> dict[str, Any]:
    """Post a message to a channel, optionally as a thread reply."""
    body: dict[str, Any] = {"channel": channel_id, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    data = await _slack("chat.postMessage", body=body)
    return {"channel": data.get("channel"), "ts": data.get("ts")}


@mcp.tool(annotations=WRITE)
async def add_reaction(
    channel_id: Annotated[str, Field(description="Channel ID of the message")],
    timestamp: Annotated[str, Field(description="Timestamp (ts) of the message")],
    emoji: Annotated[str, Field(description="Emoji name without colons, e.g. thumbsup")],
) -> dict[str, Any]:
    """Add an emoji reaction to a message."""
    await _slack(
        "reactions.add",
        body={"channel": channel_id, "timestamp": timestamp, "name": emoji.strip(":")},
    )
    return {"ok": True}


def main() -> None:
    mcp.run(
        transport="http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
