"""Security-sensitive configuration tests."""

import pytest
from pydantic import ValidationError

from deep_research.config import Settings


def test_network_transport_requires_authentication(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_HTTP", raising=False)

    with pytest.raises(ValidationError, match="AUTH_TOKEN is required"):
        Settings(tavily_api_keys="test-key", transport="http")


def test_network_transport_accepts_configured_token(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_HTTP", raising=False)
    settings = Settings(
        tavily_api_keys="test-key",
        transport="http",
        auth_token="configured-token",
    )
    assert settings.auth_token == "configured-token"


def test_explicit_unauthenticated_http_override(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    settings = Settings(
        tavily_api_keys="test-key",
        transport="http",
        allow_unauthenticated_http=True,
    )
    assert settings.allow_unauthenticated_http is True


def test_stdio_remains_unauthenticated_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    settings = Settings(tavily_api_keys="test-key", transport="stdio")
    assert settings.auth_token == ""
