import os
from dataclasses import dataclass
from pathlib import Path
import math


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    hypervisor_binary: str = "cloud-hypervisor"
    runtime_dir: Path = Path("/tmp/ch-worker")
    host_reserve_mib: int = 512
    operation_timeout: float = 10.0

    def __post_init__(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("WORKER_PORT must be between 1 and 65535")
        if self.host_reserve_mib < 0:
            raise ValueError("WORKER_HOST_RESERVE_MIB must not be negative")
        if not math.isfinite(self.operation_timeout) or self.operation_timeout <= 0:
            raise ValueError("WORKER_OPERATION_TIMEOUT must be finite and positive")

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
        )
