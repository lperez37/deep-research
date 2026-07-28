"""Configuration via environment variables."""

from __future__ import annotations

import math

from pydantic import computed_field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings are read from environment variables."""

    # Comma-separated Tavily API keys (at least 1 required)
    tavily_api_keys: str  # raw comma-separated string from env

    # Monthly credit budget per key (free tier = 1000)
    credits_per_key: int = 1000

    # Hours to skip a key after Tavily returns 429 on it. After the cooldown
    # expires the key is probed again; if it 429s again, cooldown re-extends.
    # Survives monthly rollover so a stale 429 doesn't lock the key out of the
    # new period — we always re-probe instead.
    cooldown_hours: float = 24.0

    # Key selection strategy
    routing_strategy: str = "round-robin"

    # SQLite database path for credit tracking
    db_path: str = "/data/credits.db"

    # Tavily REST API base URL
    tavily_base_url: str = "https://api.tavily.com"

    # MCP server transport
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000

    # Optional bearer token to protect this MCP server.
    # When set, clients must send this token to authenticate.
    # When empty/unset, no auth is required.
    auth_token: str = ""

    # HTTP is closed by default unless authentication is configured. Set this
    # only for a deliberately isolated development deployment.
    allow_unauthenticated_http: bool = False

    # Trust X-Forwarded-For when recording requester source IP. Keep disabled
    # unless the service is reachable only through a trusted reverse proxy.
    trust_proxy_headers: bool = False

    # Bound sensitive audit data and prune it automatically.
    audit_max_text_chars: int = 8192
    audit_retention_days: int = 90
    audit_busy_timeout_seconds: float = 0.1

    # Bound accidental parallel fan-out from one MCP client session. Set the
    # limit to 0 only when another gateway already enforces a budget.
    session_burst_limit: int = 5
    session_burst_window_seconds: float = 60.0

    model_config = {
        "env_prefix": "",
        "env_nested_delimiter": "__",
    }

    @computed_field
    @property
    def api_keys(self) -> list[str]:
        """Parse comma-separated keys into a list."""
        return [k.strip() for k in self.tavily_api_keys.split(",") if k.strip()]

    @model_validator(mode="after")
    def require_http_auth(self) -> Settings:
        """Refuse an accidentally open network transport."""
        if (
            self.transport in {"http", "sse", "streamable-http"}
            and not self.auth_token
            and not self.allow_unauthenticated_http
        ):
            raise ValueError(
                "AUTH_TOKEN is required for HTTP/SSE transport; set "
                "ALLOW_UNAUTHENTICATED_HTTP=true only for an isolated deployment"
            )
        if self.audit_max_text_chars < 256:
            raise ValueError("AUDIT_MAX_TEXT_CHARS must be at least 256")
        if self.audit_retention_days < 1:
            raise ValueError("AUDIT_RETENTION_DAYS must be at least 1")
        if not 0 <= self.audit_busy_timeout_seconds <= 1:
            raise ValueError("AUDIT_BUSY_TIMEOUT_SECONDS must be between 0 and 1")
        if self.session_burst_limit < 0:
            raise ValueError("SESSION_BURST_LIMIT cannot be negative")
        if (
            not math.isfinite(self.session_burst_window_seconds)
            or self.session_burst_window_seconds <= 0
        ):
            raise ValueError("SESSION_BURST_WINDOW_SECONDS must be greater than 0")
        return self
