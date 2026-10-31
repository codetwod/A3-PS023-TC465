"""User login, scoped JWT verification, and controller-to-worker credentials."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from pydantic import BaseModel, ConfigDict, Field

bearer = HTTPBearer(auto_error=False)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return salt + ":" + base64.b64encode(digest).decode()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class Auth:
    def __init__(self, settings):
        self.settings = settings
        if not settings.private_key_path or not settings.users_path:
            raise ValueError("CONTROLLER_PRIVATE_KEY_PATH and CONTROLLER_USERS_PATH are required")
        self.key = serialization.load_pem_private_key(settings.private_key_path.read_bytes(), password=None)
        if not isinstance(self.key, RSAPrivateKey) or self.key.key_size < 2048:
            raise ValueError("Controller requires an RSA private key of at least 2048 bits")
        self.public_key = self.key.public_key()
        self.users = json.loads(settings.users_path.read_text())
        if not isinstance(self.users, dict) or not self.users:
            raise ValueError("Users file must contain a nonempty username-to-record mapping")
        for username, user in self.users.items():
            if not isinstance(username, str) or not username.strip() or not isinstance(user, dict):
                raise ValueError("Invalid user record")
            try:
                salt, digest = user["password_hash"].split(":")
                if len(bytes.fromhex(salt)) != 16 or len(base64.b64decode(digest, validate=True)) != 32:
                    raise ValueError("Invalid password hash")
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("Invalid password hash") from exc
            if user.get("role", "user") not in ("user", "admin"):
                raise ValueError("User role must be user or admin")
        # Same-cost verification for unknown usernames.
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        self.heartbeat_keys = {}
        for worker_id in settings.workers:
            key_path = settings.heartbeat_public_key_paths.get(worker_id) or settings.heartbeat_public_key_path
            if not key_path:
                raise ValueError("A heartbeat public key is required for each configured worker")
            key = serialization.load_pem_public_key(Path(key_path).read_bytes())
            if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                raise ValueError("Heartbeat verification requires an RSA public key of at least 2048 bits")
            self.heartbeat_keys[worker_id] = key

    def token(self, subject, audience, lifetime, **extra):
        timestamp = int(datetime.now(timezone.utc).timestamp())
        return jwt.encode({"iss": self.settings.jwt_issuer, "aud": audience, "sub": subject,
                           "iat": timestamp, "exp": timestamp + lifetime, **extra}, self.key, algorithm="RS256")

    def login(self, payload):
        user = self.users.get(payload.username)
        stored = user["password_hash"] if user else self.dummy_hash
        salt = stored.split(":")[0]
        valid = hmac.compare_digest(password_hash(payload.password, salt), stored)
        if not valid or not user or user.get("disabled", False):
            raise HTTPException(401, "Invalid username or password", headers={"WWW-Authenticate": "Bearer"})
        return {"access_token": self.token(payload.username, self.settings.jwt_audience,
                                           self.settings.token_lifetime, token_type="user"),
                "token_type": "bearer", "expires_in": self.settings.token_lifetime}

    def decode(self, token, key, issuer, audience):
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=audience,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        for name in ("iat", "exp", "nbf"):
            if name in claims and (type(claims[name]) not in (int, float) or not math.isfinite(claims[name])):
                raise jwt.InvalidTokenError("Invalid timestamp")
        if claims["exp"] <= claims["iat"] or not isinstance(claims["sub"], str) or not claims["sub"].strip():
            raise jwt.InvalidTokenError("Invalid lifetime or subject")
        return claims

    def user(self, token):
        claims = self.decode(token, self.public_key, self.settings.jwt_issuer, self.settings.jwt_audience)
        user = self.users.get(claims["sub"])
        if claims.get("token_type") != "user" or not user or user.get("disabled", False):
            raise jwt.InvalidTokenError("Invalid user")
        return {"sub": claims["sub"], "role": user.get("role", "user")}

    def worker_token(self, worker_id):
        return self.token("controller", self.settings.worker_audiences.get(worker_id, worker_id), 300)


async def require_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    try:
        if credentials is None:
            raise jwt.InvalidTokenError()
        return request.app.state.auth.user(credentials.credentials)
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
        raise HTTPException(401, "Invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"}) from None


async def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


async def require_worker(worker_id: str, request: Request,
                         credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    auth = request.app.state.auth
    try:
        if credentials is None or worker_id not in auth.heartbeat_keys:
            raise jwt.InvalidTokenError()
        claims = auth.decode(credentials.credentials, auth.heartbeat_keys[worker_id],
                             auth.settings.heartbeat_issuer, auth.settings.heartbeat_audience)
        if claims["sub"] != worker_id or worker_id not in auth.settings.workers:
            raise jwt.InvalidTokenError()
        return claims
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
        raise HTTPException(401, "Invalid worker credential", headers={"WWW-Authenticate": "Bearer"}) from None
