"""Per-request HTTP client factory.

A new client is built for every tool call, carrying that caller's credentials.
Nothing is cached or shared between callers, so users can never see each
other's data.
"""

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def make_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=False,  # never forward a caller's token to a redirect target
    )
