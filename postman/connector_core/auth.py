"""Read the caller's credentials from the current request's headers.

Nothing is stored. Each call reads the headers of the request it belongs to,
so one caller can never see another caller's values.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers


def _headers() -> dict[str, str]:
    # include_all=True is required: FastMCP strips 'Authorization' by default.
    return get_http_headers(include_all=True)


def get_optional_header(name: str) -> str | None:
    value = _headers().get(name.lower(), "").strip()
    return value or None


def get_api_key() -> str:
    """The caller's Postman API key.

    Accepted as 'X-Api-Key: PMAK-...' (Postman's own header) or as
    'Authorization: Bearer PMAK-...' (the style Postman's hosted MCP uses).
    """
    key = get_optional_header("x-api-key")
    if key:
        return key
    auth = get_optional_header("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        raise ToolError("Authorization header must look like 'Bearer <postman-api-key>'.")
    raise ToolError("Missing Postman API key: send 'X-Api-Key: <key>' (or 'Authorization: Bearer <key>').")
