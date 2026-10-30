"""Best-effort worker-to-controller reporting, independent of VM request handling."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging

import httpx

from worker.api import WorkerBackend
from worker.config import Settings

logger = logging.getLogger(__name__)


class HeartbeatReporter:
    def __init__(self, settings: Settings, backend: WorkerBackend):
        self.settings = settings
        self.backend = backend
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("Heartbeat reporter is already started")
        self.task = asyncio.create_task(self.run(), name="worker-heartbeat")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def send_once(self, client: httpx.AsyncClient) -> None:
        # Reload the externally issued JWT so it can be renewed without restarting.
        token = self.settings.heartbeat_token_path.read_text().strip()
        if not token or any(char.isspace() for char in token):
            raise ValueError("Heartbeat credential file is empty or malformed")
        async with asyncio.timeout(self.settings.heartbeat_timeout):
            resources = await self.backend.resources()
            payload = {
                "worker_id": self.settings.worker_id,
                "worker_url": self.settings.advertised_url,
                "status": "ready",
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "resources": resources,
            }
            response = await client.post(self.settings.heartbeat_url, json=payload,
                                         headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()

    async def run(self) -> None:
        failures = 0
        async with httpx.AsyncClient(timeout=self.settings.heartbeat_timeout,
                                    follow_redirects=False, trust_env=False) as client:
            while True:
                try:
                    await self.send_once(client)
                    if failures:
                        logger.info("Controller heartbeat connection recovered")
                    failures = 0
                    delay = self.settings.heartbeat_interval
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    # Never log credentials, request bodies or controller response bodies.
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    logger.warning("Heartbeat failed (%s, HTTP %s); retrying", type(exc).__name__, status)
                    delay = min(self.settings.heartbeat_interval * 2 ** min(failures - 1, 3),
                                self.settings.heartbeat_interval * 8)
                await asyncio.sleep(delay)