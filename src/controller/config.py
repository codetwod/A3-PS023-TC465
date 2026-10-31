import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 9000
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "controller:"
    workers: dict[str, str] = field(default_factory=dict)
    worker_token_paths: dict[str, str] = field(default_factory=dict)
    request_timeout: float = 10
    heartbeat_stale_seconds: float = 30
    reconcile_interval: float = 30
    private_key_path: Path | None = None
    users_path: Path | None = None
    heartbeat_public_key_path: Path | None = None
    heartbeat_public_key_paths: dict[str, str] = field(default_factory=dict)
    jwt_issuer: str = "vm-controller"
    jwt_audience: str = "vm-controller-api"
    heartbeat_issuer: str = "worker-heartbeats"
    heartbeat_audience: str = "vm-controller-heartbeats"
    worker_audiences: dict[str, str] = field(default_factory=dict)
    token_lifetime: int = 900

    def __post_init__(self):
        import math
        if not 1 <= self.port <= 65535 or not self.redis_prefix:
            raise ValueError("Invalid controller port or Redis prefix")
        if not 60 <= self.token_lifetime <= 3600:
            raise ValueError("Token lifetime must be between 60 and 3600 seconds")
        if not all(v.strip() for v in (self.jwt_issuer, self.jwt_audience,
                                      self.heartbeat_issuer, self.heartbeat_audience)):
            raise ValueError("JWT issuers and audiences must not be empty")
        for value in (self.request_timeout, self.heartbeat_stale_seconds, self.reconcile_interval):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Controller timeouts must be finite and positive")
        for worker_id, url in self.workers.items():
            parsed = urlsplit(url)
            if (not worker_id or parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Workers require an ID and HTTP(S) URL without credentials/query/fragment")

    @classmethod
    def from_env(cls):
        return cls(
            host=os.getenv("CONTROLLER_HOST", "127.0.0.1"),
            port=int(os.getenv("CONTROLLER_PORT", "9000")),
            redis_url=os.getenv("CONTROLLER_REDIS_URL", "redis://127.0.0.1:6379/0"),
            redis_prefix=os.getenv("CONTROLLER_REDIS_PREFIX", "controller:"),
            workers=json.loads(os.getenv("CONTROLLER_WORKERS", "{}")),
            worker_token_paths=json.loads(os.getenv("CONTROLLER_WORKER_TOKEN_PATHS", "{}")),
            request_timeout=float(os.getenv("CONTROLLER_REQUEST_TIMEOUT", "10")),
            heartbeat_stale_seconds=float(os.getenv("CONTROLLER_HEARTBEAT_STALE_SECONDS", "30")),
            reconcile_interval=float(os.getenv("CONTROLLER_RECONCILE_INTERVAL", "30")),
            private_key_path=Path(os.environ["CONTROLLER_PRIVATE_KEY_PATH"]) if os.getenv("CONTROLLER_PRIVATE_KEY_PATH") else None,
            users_path=Path(os.environ["CONTROLLER_USERS_PATH"]) if os.getenv("CONTROLLER_USERS_PATH") else None,
            heartbeat_public_key_path=Path(os.environ["CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH"]) if os.getenv("CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH") else None,
            heartbeat_public_key_paths=json.loads(os.getenv("CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATHS", "{}")),
            jwt_issuer=os.getenv("CONTROLLER_JWT_ISSUER", "vm-controller"),
            jwt_audience=os.getenv("CONTROLLER_JWT_AUDIENCE", "vm-controller-api"),
            heartbeat_issuer=os.getenv("CONTROLLER_HEARTBEAT_ISSUER", "worker-heartbeats"),
            heartbeat_audience=os.getenv("CONTROLLER_HEARTBEAT_AUDIENCE", "vm-controller-heartbeats"),
            worker_audiences=json.loads(os.getenv("CONTROLLER_WORKER_AUDIENCES", "{}")),
            token_lifetime=int(os.getenv("CONTROLLER_TOKEN_LIFETIME", "900")),
        )
