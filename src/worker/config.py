import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        port = int(os.environ.get("WORKER_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("WORKER_PORT must be between 1 and 65535")
        return cls(host=os.environ.get("WORKER_HOST", "127.0.0.1"), port=port)