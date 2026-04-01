"""Configuration via environment variables."""

from __future__ import annotations

from pydantic import computed_field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings are read from environment variables."""

    # Comma-separated Tavily API keys (at least 1 required)
    tavily_api_keys: str  # raw comma-separated string from env

    # Monthly credit budget per key (free tier = 1000)
    credits_per_key: int = 1000

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

    model_config = {
        "env_prefix": "",
        "env_nested_delimiter": "__",
    }

    @computed_field
    @property
    def api_keys(self) -> list[str]:
        """Parse comma-separated keys into a list."""
        return [k.strip() for k in self.tavily_api_keys.split(",") if k.strip()]
