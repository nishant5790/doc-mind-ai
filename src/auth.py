"""
src/auth.py — Azure AD (Entra ID) JWT validation for FastAPI.

The frontend obtains an access token via MSAL and sends it as
`Authorization: Bearer <jwt>`. We validate it against the tenant's
JWKS endpoint and extract the user identifier (`oid`).

Set `DOCMIND_DISABLE_AUTH=true` in `.env` to bypass auth during local
development and notebook testing — every request is then attributed to
`user_id="anonymous"`.
"""

from __future__ import annotations

import logging
from typing import Optional

import jwt
import requests
from fastapi import Header, HTTPException, status

import config

log = logging.getLogger(__name__)


class AuthService:
    """Lightweight JWT validator backed by Azure AD JWKS."""

    def __init__(
        self,
        tenant_id: Optional[str] = config.AZURE_TENANT_ID,
        audience: Optional[str] = config.AZURE_API_AUDIENCE,
        disabled: bool = config.DISABLE_AUTH,
    ) -> None:
        self.tenant_id = tenant_id
        self.audience = audience
        self.disabled = disabled
        self._jwks_client: Optional[jwt.PyJWKClient] = None
        self._issuer: Optional[str] = None

        if not disabled and tenant_id:
            jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
            self._jwks_client = jwt.PyJWKClient(jwks_url)
            self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
            log.info("Azure AD auth enabled (tenant=%s)", tenant_id)
        else:
            log.warning("Azure AD auth DISABLED — all requests are anonymous")

    # ------------------------------------------------------------------
    def validate(self, authorization: Optional[str]) -> str:
        """Validate `Bearer <jwt>` header. Returns user_id (oid claim)."""
        if self.disabled:
            return "anonymous"

        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        token = authorization.split(" ", 1)[1].strip()

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self._issuer,
            )
        except Exception as e:
            log.warning("JWT validation failed: %s", e)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

        return payload.get("oid") or payload.get("sub") or "anonymous"


# Singleton used by FastAPI dependency
_auth_service: Optional[AuthService] = None


def get_auth() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service


def current_user(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency — returns the authenticated user_id."""
    return get_auth().validate(authorization)
