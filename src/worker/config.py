import os
from dataclasses import dataclass
from pathlib import Path
import math
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    hypervisor_binary: str = "cloud-hypervisor"
    runtime_dir: Path = Path("/tmp/ch-worker")
    host_reserve_mib: int = 512
    operation_timeout: float = 10.0
    jwt_public_key_path: Path | None = None
    jwt_issuer: str = "vm-controller"
    jwt_audience: str = "worker"
    jwt_subject: str = "controller"
    heartbeat_url: str | None = None
    worker_id: str = "worker-1"
    advertised_url: str | None = None
    heartbeat_token_path: Path | None = None
    heartbeat_private_key_path: Path | None = None
    heartbeat_issuer: str = "worker-heartbeats"
    heartbeat_audience: str = "vm-controller-heartbeats"
    heartbeat_interval: float = 10.0
    heartbeat_timeout: float = 5.0

    def __post_init__(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("WORKER_PORT must be between 1 and 65535")
        if self.host_reserve_mib < 0:
            raise ValueError("WORKER_HOST_RESERVE_MIB must not be negative")
        if not math.isfinite(self.operation_timeout) or self.operation_timeout <= 0:
            raise ValueError("WORKER_OPERATION_TIMEOUT must be finite and positive")
        for value in (self.heartbeat_interval, self.heartbeat_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Heartbeat interval and timeout must be finite and positive")
        if self.heartbeat_url is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.worker_id):
                raise ValueError("WORKER_ID must be a valid worker identifier")
            if (self.heartbeat_token_path is None and self.heartbeat_private_key_path is None) or not self.advertised_url:
                raise ValueError("Heartbeat requires a token file or signing key and WORKER_ADVERTISED_URL")
            if self.heartbeat_token_path is not None and self.heartbeat_private_key_path is not None:
                raise ValueError("Configure only one heartbeat credential source")
            if not self.heartbeat_issuer.strip() or not self.heartbeat_audience.strip():
                raise ValueError("Heartbeat issuer and audience must not be empty")
            for url in (self.heartbeat_url, self.advertised_url):
                parsed = urlsplit(url)
                if (parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or parsed.query or parsed.fragment):
                    raise ValueError("Heartbeat URLs must be HTTP(S) URLs without credentials, query or fragment")
                if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError("Use HTTPS for heartbeat URLs outside loopback")

    @classmethod
    def from_env(cls) -> "Settings":
        port = int(os.environ.get("WORKER_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("WORKER_PORT must be between 1 and 65535")
        return cls(
            host=os.environ.get("WORKER_HOST", "127.0.0.1"), port=port,
            hypervisor_binary=os.environ.get("WORKER_HYPERVISOR_BINARY", "cloud-hypervisor"),
            runtime_dir=Path(os.environ.get("WORKER_RUNTIME_DIR", "/tmp/ch-worker")),
            host_reserve_mib=int(os.environ.get("WORKER_HOST_RESERVE_MIB", "512")),
            operation_timeout=float(os.environ.get("WORKER_OPERATION_TIMEOUT", "10")),
            jwt_public_key_path=Path(os.environ["WORKER_JWT_PUBLIC_KEY_PATH"])
                if os.environ.get("WORKER_JWT_PUBLIC_KEY_PATH") else None,
            jwt_issuer=os.environ.get("WORKER_JWT_ISSUER", "vm-controller"),
            jwt_audience=os.environ.get("WORKER_JWT_AUDIENCE", "worker"),
            jwt_subject=os.environ.get("WORKER_JWT_SUBJECT", "controller"),
            heartbeat_url=os.environ.get("WORKER_HEARTBEAT_URL") or None,
            worker_id=os.environ.get("WORKER_ID", "worker-1"),
            advertised_url=os.environ.get("WORKER_ADVERTISED_URL") or None,
            heartbeat_token_path=Path(os.environ["WORKER_HEARTBEAT_TOKEN_PATH"])
                if os.environ.get("WORKER_HEARTBEAT_TOKEN_PATH") else None,
            heartbeat_private_key_path=Path(os.environ["WORKER_HEARTBEAT_PRIVATE_KEY_PATH"])
                if os.environ.get("WORKER_HEARTBEAT_PRIVATE_KEY_PATH") else None,
            heartbeat_issuer=os.environ.get("WORKER_HEARTBEAT_ISSUER", "worker-heartbeats"),
            heartbeat_audience=os.environ.get("WORKER_HEARTBEAT_AUDIENCE", "vm-controller-heartbeats"),
            heartbeat_interval=float(os.environ.get("WORKER_HEARTBEAT_INTERVAL", "10")),
            heartbeat_timeout=float(os.environ.get("WORKER_HEARTBEAT_TIMEOUT", "5")),
        )
