"""Bearer-token authentication for HTTP MCP transports."""

from __future__ import annotations

import hashlib
import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class ConfiguredTokenVerifier(TokenVerifier):
    """Validate one configured bearer token without timing-sensitive comparison."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("A non-empty bearer token is required")
        super().__init__()
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate_digest = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(self._token_digest, candidate_digest):
            return None
        return AccessToken(
            token=token,
            client_id="configured-bearer-token",
            scopes=[],
            claims={"auth_method": "configured_bearer_token"},
        )
