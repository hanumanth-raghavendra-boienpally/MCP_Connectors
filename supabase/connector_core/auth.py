"""Read the caller's credentials and settings from the current request's headers.

Nothing is stored. Each call reads the headers of the request it belongs to,
so one caller can never see another caller's values.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers


def get_optional_header(name: str) -> str | None:
    # include_all=True is required: FastMCP strips 'Authorization' by default.
    value = get_http_headers(include_all=True).get(name.lower(), "").strip()
    return value or None


def get_bearer_token() -> str:
    raw = get_optional_header("authorization")
    if not raw:
        raise ToolError("Missing required header 'authorization' (Bearer <supabase-access-token>).")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ToolError("Authorization header must look like 'Bearer <supabase-access-token>'.")
    return token.strip()


def get_flag(name: str) -> bool:
    """True for header values like 'true', '1', 'yes', 'on'."""
    return (get_optional_header(name) or "").lower() in {"true", "1", "yes", "on"}
