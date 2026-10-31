import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


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

    def __post_init__(self):
        import math
        if not 1 <= self.port <= 65535 or not self.redis_prefix:
            raise ValueError("Invalid controller port or Redis prefix")
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
        )
