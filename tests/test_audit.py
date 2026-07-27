"""Tests for requester metadata attribution."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from deep_research import audit


class FakeContext:
    request_id = "request-42"
    session_id = "session-7"

    def __init__(self, *, transport: str = "streamable-http") -> None:
        self.transport = transport
        self.session = SimpleNamespace(
            client_params=SimpleNamespace(
                clientInfo=SimpleNamespace(name="mcp-client", version="2.0")
            )
        )


def _request(headers: dict[str, str], client=("198.51.100.9", 1234)) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
            "client": client,
            "server": ("search.example", 443),
            "root_path": "",
        }
    )


def test_explicit_requester_headers_override_client_info(monkeypatch) -> None:
    request = _request(
        {
            "X-Requester-ID": "team-search",
            "X-Requester-Hostname": "agent-host-3",
            "X-Requester-Application": "Hermes",
            "X-Requester-Application-Version": "4.5.6",
            "User-Agent": "python-httpx/0.28",
        }
    )
    monkeypatch.setattr(audit, "get_http_request", lambda: request)

    metadata = audit.requester_metadata(FakeContext())

    assert metadata.request_id == "request-42"
    assert metadata.session_id == "session-7"
    assert metadata.requester_id == "team-search"
    assert metadata.hostname == "agent-host-3"
    assert metadata.application == "Hermes"
    assert metadata.application_version == "4.5.6"
    assert metadata.source_ip == "198.51.100.9"
    assert metadata.user_agent == "python-httpx/0.28"
    assert metadata.transport == "streamable-http"


def test_client_info_is_application_fallback(monkeypatch) -> None:
    monkeypatch.setattr(audit, "get_http_request", lambda: _request({}))

    metadata = audit.requester_metadata(FakeContext())

    assert metadata.application == "mcp-client"
    assert metadata.application_version == "2.0"
    assert metadata.hostname is None


def test_forwarded_ip_only_trusted_when_configured(monkeypatch) -> None:
    request = _request({"X-Forwarded-For": "203.0.113.8, 10.0.0.2"})
    monkeypatch.setattr(audit, "get_http_request", lambda: request)

    untrusted = audit.requester_metadata(FakeContext(), trust_proxy_headers=False)
    trusted = audit.requester_metadata(FakeContext(), trust_proxy_headers=True)

    assert untrusted.source_ip == "198.51.100.9"
    assert trusted.source_ip == "203.0.113.8"


def test_malformed_forwarded_ip_is_rejected(monkeypatch) -> None:
    request = _request({"X-Forwarded-For": "not-an-ip, 203.0.113.8"})
    monkeypatch.setattr(audit, "get_http_request", lambda: request)

    metadata = audit.requester_metadata(FakeContext(), trust_proxy_headers=True)

    assert metadata.source_ip == "198.51.100.9"


def test_stdio_uses_local_hostname_and_client_info(monkeypatch) -> None:
    def no_http_request():
        raise RuntimeError("No active HTTP request found")

    monkeypatch.setattr(audit, "get_http_request", no_http_request)
    monkeypatch.setattr(audit.socket, "gethostname", lambda: "local-machine")

    metadata = audit.requester_metadata(FakeContext(transport="stdio"))

    assert metadata.hostname == "local-machine"
    assert metadata.application == "mcp-client"
    assert metadata.source_ip is None


def test_untrusted_header_values_are_bounded(monkeypatch) -> None:
    request = _request({"X-Requester-Application": "x" * 2000})
    monkeypatch.setattr(audit, "get_http_request", lambda: request)

    metadata = audit.requester_metadata(FakeContext())

    assert metadata.application == "x" * 1024


def test_no_session_context_values_are_optional(monkeypatch) -> None:
    class IncompleteContext(FakeContext):
        @property
        def request_id(self):
            raise RuntimeError("not initialised")

        @property
        def session_id(self):
            raise RuntimeError("not initialised")

    monkeypatch.setattr(audit, "get_http_request", lambda: _request({}))
    metadata = audit.requester_metadata(IncompleteContext())

    assert metadata.request_id is None
    assert metadata.session_id is None
