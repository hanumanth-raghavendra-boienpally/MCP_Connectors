"""Read caller credentials from the current request's headers.

Rules every connector follows:
  * Credentials come from the request, never from env vars or config files.
  * FastMCP strips the Authorization header unless include_all=True, so we
    always pass it. Without it a Bearer token silently never arrives.
  * Error messages never contain header values.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers


def get_header(name: str, *, required: bool = True) -> str | None:
    """Return one header value (case-insensitive) from the current request."""
    headers = get_http_headers(include_all=True)
    value = headers.get(name.lower(), "").strip()
    if not value:
        if required:
            raise ToolError(f"Missing required header '{name}'.")
        return None
    return value


def get_bearer_token() -> str:
    """Return the token from 'Authorization: Bearer <token>'."""
    raw = get_header("authorization")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ToolError("Authorization header must look like 'Bearer <token>'.")
    return token.strip()
