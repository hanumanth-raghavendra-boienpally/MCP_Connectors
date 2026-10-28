"""Read the caller's credentials from the current request's headers.

Nothing is stored. Each call reads the headers of the request it belongs to,
so one caller can never see another caller's values.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

_AUTH_SCHEMES = {"apikey", "basic", "bearer"}


def get_header(name: str) -> str:
    # include_all=True is required: FastMCP strips 'Authorization' by default.
    headers = get_http_headers(include_all=True)
    value = headers.get(name.lower(), "").strip()
    if not value:
        raise ToolError(f"Missing required header '{name}'.")
    return value


def get_authorization() -> str:
    """Return the caller's Authorization header (ApiKey, Basic or Bearer)."""
    raw = get_header("authorization")
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() not in _AUTH_SCHEMES or not rest.strip():
        raise ToolError(
            "Authorization header must look like 'ApiKey <key>', "
            "'Basic <credentials>' or 'Bearer <token>'."
        )
    return f"{scheme} {rest.strip()}"
