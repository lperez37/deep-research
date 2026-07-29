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


def test_fallback_requires_only_dataforseo_credentials() -> None:
    settings = Settings(
        tavily_api_keys="test-key",
        fallback_enabled=True,
        dataforseo_auth="configured-auth",
    )

    assert settings.fallback_enabled is True


def test_fallback_rejects_missing_dataforseo_credentials() -> None:
    with pytest.raises(ValidationError, match="DATAFORSEO_AUTH is missing"):
        Settings(tavily_api_keys="test-key", fallback_enabled=True)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_burst_limit", -1, "SESSION_BURST_LIMIT cannot be negative"),
        (
            "session_burst_window_seconds",
            0,
            "SESSION_BURST_WINDOW_SECONDS must be greater than 0",
        ),
        (
            "session_burst_window_seconds",
            float("nan"),
            "SESSION_BURST_WINDOW_SECONDS must be greater than 0",
        ),
        (
            "session_burst_window_seconds",
            float("inf"),
            "SESSION_BURST_WINDOW_SECONDS must be greater than 0",
        ),
    ],
)
def test_invalid_burst_settings_are_rejected(field, value, message) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(tavily_api_keys="test-key", **{field: value})
