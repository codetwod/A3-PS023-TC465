"""Verify controller-issued JWTs. Signing keys never belong on the worker."""
from typing import Annotated

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from worker.config import Settings

bearer = HTTPBearer(auto_error=False, description="Controller-issued RS256 JWT")


class JWTVerifier:
    def __init__(self, settings: Settings):
        if settings.jwt_public_key_path is None:
            raise ValueError("WORKER_JWT_PUBLIC_KEY_PATH is required")
        if not all(value.strip() for value in
                   (settings.jwt_issuer, settings.jwt_audience, settings.jwt_subject)):
            raise ValueError("JWT issuer, audience and subject must not be empty")
        self.key = serialization.load_pem_public_key(settings.jwt_public_key_path.read_bytes())
        if not isinstance(self.key, RSAPublicKey) or self.key.key_size < 2048:
            raise ValueError("JWT verification requires an RSA public key of at least 2048 bits")
        self.settings = settings

    def verify(self, token: str) -> dict:
        claims = jwt.decode(
            token, self.key, algorithms=["RS256"],
            issuer=self.settings.jwt_issuer, audience=self.settings.jwt_audience,
            subject=self.settings.jwt_subject,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        # NumericDate claims must be numbers, not strings/bools or nonfinite values.
        import math
        for name in ("iat", "exp", "nbf"):
            if name in claims and (type(claims[name]) not in (int, float)
                                   or not math.isfinite(claims[name])):
                raise jwt.InvalidTokenError("Invalid token timestamp")
        if claims["exp"] <= claims["iat"]:
            raise jwt.InvalidTokenError("Invalid token lifetime")
        return claims


async def require_controller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict:
    unauthorized = HTTPException(401, "Invalid or missing bearer token",
                                 headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise unauthorized
    verifier = getattr(request.app.state, "jwt_verifier", None)
    if verifier is None:
        raise HTTPException(503, "Authentication is not ready")
    try:
        claims = verifier.verify(credentials.credentials)
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
        raise unauthorized from None
    request.state.controller_claims = claims
    return claims