"""Requester metadata extraction for the persistent request audit log."""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from ipaddress import ip_address

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request


@dataclass(frozen=True)
class RequesterMetadata:
    """Attribution captured for one MCP tool call.

    Explicit ``X-Requester-*`` headers take precedence over MCP client metadata.
    They are self-declared identifiers, not authenticated identities.
    """

    request_id: str | None = None
    session_id: str | None = None
    requester_id: str | None = None
    hostname: str | None = None
    application: str | None = None
    application_version: str | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    transport: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def requester_metadata(
    ctx: Context, *, trust_proxy_headers: bool = False
) -> RequesterMetadata:
    """Extract requester details without collecting credentials or raw headers."""
    request_id = _safe_context_value(ctx, "request_id")
    session_id = _safe_context_value(ctx, "session_id")
    transport = ctx.transport

    application = None
    application_version = None
    params = getattr(ctx.session, "client_params", None)
    client_info = getattr(params, "clientInfo", None) if params else None
    if client_info is not None:
        application = _clean(getattr(client_info, "name", None))
        application_version = _clean(getattr(client_info, "version", None))

    requester_id = None
    hostname = socket.gethostname() if transport == "stdio" else None
    source_ip = None
    user_agent = None

    try:
        request = get_http_request()
    except RuntimeError:
        request = None

    if request is not None:
        headers = request.headers
        requester_id = _clean(headers.get("x-requester-id"))
        hostname = _clean(headers.get("x-requester-hostname")) or hostname
        application = _clean(headers.get("x-requester-application")) or application
        application_version = (
            _clean(headers.get("x-requester-application-version"))
            or application_version
        )
        user_agent = _clean(headers.get("user-agent"))

        if request.client:
            source_ip = _valid_ip(request.client.host)
        if trust_proxy_headers:
            forwarded_for = _clean(headers.get("x-forwarded-for"))
            if forwarded_for:
                source_ip = _valid_ip(forwarded_for.split(",", 1)[0]) or source_ip

    return RequesterMetadata(
        request_id=request_id,
        session_id=session_id,
        requester_id=requester_id,
        hostname=hostname,
        application=application,
        application_version=application_version,
        source_ip=source_ip,
        user_agent=user_agent,
        transport=transport,
    )


def _safe_context_value(ctx: Context, attribute: str) -> str | None:
    try:
        return _clean(getattr(ctx, attribute))
    except RuntimeError:
        return None


def _clean(value: object, *, max_length: int = 1024) -> str | None:
    """Normalise metadata and bound untrusted header values."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def _valid_ip(value: object) -> str | None:
    """Return a canonical IP address or reject malformed attribution input."""
    cleaned = _clean(value, max_length=64)
    if cleaned is None:
        return None
    try:
        return str(ip_address(cleaned))
    except ValueError:
        return None
