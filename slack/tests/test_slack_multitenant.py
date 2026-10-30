"""End-to-end multi-tenant tests for the Slack connector.

The real connector runs as a subprocess over streamable HTTP and talks to a
fake Slack. We check that each caller's token reaches Slack for that caller's
calls only, even when many callers hit the server at the same time.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from tests import fake_slack

ROOT = Path(__file__).resolve().parent.parent
FAKE_PORT = 8791
CONNECTOR_PORT = 8792
URL = f"http://127.0.0.1:{CONNECTOR_PORT}/mcp"


@pytest.fixture(scope="module")
def connector(tmp_path_factory):
    fake = fake_slack.start(FAKE_PORT)
    env = {
        **os.environ,
        "SLACK_API_BASE": f"http://127.0.0.1:{FAKE_PORT}",
        "HOST": "127.0.0.1",
        "PORT": str(CONNECTOR_PORT),
    }
    # Send the server's output to a FILE, not a pipe. A pipe nobody reads fills
    # up (about 4 KB on Windows), which blocks the server and hangs every test.
    log_path = tmp_path_factory.mktemp("logs") / "connector.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "connectors.slack.server"],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
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
    fake.should_exit = True
    log = log_path.read_text(errors="replace")
    # Secrets must never appear in server logs.
    assert "xoxb-user-a" not in log and "xoxb-user-b" not in log


def _client(token: str | None, extra: dict | None = None) -> Client:
    headers = dict(extra or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # A timeout makes a stuck call fail loudly instead of hanging forever.
    return Client(StreamableHttpTransport(URL, headers=headers), timeout=20)


async def _channel_names(token: str) -> list[str]:
    async with _client(token) as c:
        result = await c.call_tool("list_channels", {})
        return [ch["name"] for ch in result.data["channels"]]


def test_tools_are_listed_with_read_only_hints(connector):
    async def run():
        async with _client("xoxb-user-a") as c:
            return {t.name: t for t in await c.list_tools()}

    tools = asyncio.run(run())
    assert {"list_channels", "post_message", "get_channel_history"} <= set(tools)
    assert tools["list_channels"].annotations.read_only_hint is True
    assert tools["post_message"].annotations.read_only_hint is False


def test_each_caller_sees_only_their_own_workspace(connector):
    assert asyncio.run(_channel_names("xoxb-user-a")) == ["a-general", "a-random"]
    assert asyncio.run(_channel_names("xoxb-user-b")) == ["b-general"]


def test_concurrent_callers_never_cross(connector):
    async def run():
        jobs = []
        for _ in range(15):
            jobs.append(_channel_names("xoxb-user-a"))
            jobs.append(_channel_names("xoxb-user-b"))
        return await asyncio.gather(*jobs)

    results = asyncio.run(run())
    for i, names in enumerate(results):
        assert names == (["a-general", "a-random"] if i % 2 == 0 else ["b-general"])


def test_token_reaching_slack_matches_the_caller(connector):
    fake_slack.CALLS.clear()

    async def run():
        async with _client("xoxb-user-b") as c:
            await c.call_tool("get_channel_history", {"channel_id": "CB1"})

    asyncio.run(run())
    history_calls = [c for c in fake_slack.CALLS if c["path"] == "/conversations.history"]
    assert history_calls and all(c["token"] == "xoxb-user-b" for c in history_calls)


def test_write_tool_posts_as_the_caller(connector):
    fake_slack.CALLS.clear()

    async def run():
        async with _client("xoxb-user-a") as c:
            return await c.call_tool("post_message", {"channel_id": "CA1", "text": "hi"})

    result = asyncio.run(run())
    assert result.data["ts"] == "99.0"
    assert [c["token"] for c in fake_slack.CALLS if c["path"] == "/chat.postMessage"] == ["xoxb-user-a"]


def test_missing_authorization_header_gives_clear_error(connector):
    async def run():
        async with _client(None) as c:
            await c.call_tool("list_channels", {})

    with pytest.raises(ToolError, match="Missing required header 'authorization'"):
        asyncio.run(run())


def test_malformed_authorization_header_gives_clear_error(connector):
    async def run():
        async with Client(
            StreamableHttpTransport(URL, headers={"Authorization": "xoxb-user-a"}), timeout=20
        ) as c:
            await c.call_tool("list_channels", {})

    with pytest.raises(ToolError, match="Bearer"):
        asyncio.run(run())


def test_invalid_token_surfaces_slack_error_without_leaking_token(connector):
    async def run():
        async with _client("xoxb-not-a-real-token") as c:
            await c.call_tool("list_channels", {})

    with pytest.raises(ToolError) as exc:
        asyncio.run(run())
    assert "invalid_auth" in str(exc.value)
    assert "xoxb-not-a-real-token" not in str(exc.value)


def test_rate_limit_is_reported_with_retry_after(connector):
    async def run():
        async with _client("xoxb-user-a") as c:
            await c.call_tool("list_users", {})

    with pytest.raises(ToolError, match="Retry after 7"):
        asyncio.run(run())


def test_redirects_are_never_followed(connector):
    # A redirect could carry the caller's credentials to another address. The
    # fake redirects to /trap; the connector must refuse and never reach it.
    fake_slack.CALLS.clear()

    async def run():
        async with _client("xoxb-user-a") as c:
            await c.call_tool("get_channel_history", {"channel_id": "CREDIRECT"})

    with pytest.raises(ToolError, match="redirect"):
        asyncio.run(run())
    assert not [c for c in fake_slack.CALLS if c["path"] == "/trap"]
