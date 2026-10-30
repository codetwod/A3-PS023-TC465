import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from worker.config import Settings
from worker.heartbeat import HeartbeatReporter
from worker.main import create_app


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_path = Path(self.temp.name) / "heartbeat.jwt"
        self.token_path.write_text("test-token")
        self.settings = Settings(heartbeat_url="http://127.0.0.1:9000/workers/worker-1/heartbeat",
                                 advertised_url="http://127.0.0.1:8000", heartbeat_token_path=self.token_path)
        self.backend = SimpleNamespace(resources=AsyncMock(return_value={"allocatable_memory_mib": 1024}))
        self.reporter = HeartbeatReporter(self.settings, self.backend)

    async def test_payload_and_token_reload(self):
        requests = []

        def handler(request):
            import json
            requests.append((request, json.loads(request.content)))
            return httpx.Response(204)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await self.reporter.send_once(client)
            self.token_path.write_text("renewed-token\n")
            await self.reporter.send_once(client)
        self.assertEqual(requests[0][0].method, "POST")
        self.assertEqual(str(requests[0][0].url), self.settings.heartbeat_url)
        self.assertEqual(requests[0][0].headers["authorization"], "Bearer test-token")
        self.assertEqual(requests[1][0].headers["authorization"], "Bearer renewed-token")
        self.assertEqual(requests[0][1]["worker_id"], "worker-1")
        self.assertEqual(requests[0][1]["status"], "ready")
        self.assertEqual(requests[0][1]["resources"], {"allocatable_memory_mib": 1024})
        self.assertIn("sent_at", requests[0][1])

    async def test_rejects_errors_and_redirects(self):
        for status in (302, 401, 404, 500):
            with self.subTest(status=status):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status))) as client:
                    with self.assertRaises(httpx.HTTPStatusError):
                        await self.reporter.send_once(client)

    async def test_missing_or_empty_credential(self):
        async with httpx.AsyncClient() as client:
            self.token_path.write_text("")
            with self.assertRaises(ValueError):
                await self.reporter.send_once(client)
            self.token_path.unlink()
            with self.assertRaises(FileNotFoundError):
                await self.reporter.send_once(client)

    async def test_timeout_includes_resource_collection(self):
        reporter = HeartbeatReporter(replace(self.settings, heartbeat_timeout=0.01), self.backend)

        async def blocked():
            await asyncio.Event().wait()

        self.backend.resources.side_effect = blocked
        async with httpx.AsyncClient() as client:
            with self.assertRaises(TimeoutError):
                await reporter.send_once(client)

    async def test_retry_backoff_and_recovery(self):
        self.reporter.send_once = AsyncMock(side_effect=[OSError(), OSError(), None])
        sleeper = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with patch("worker.heartbeat.asyncio.sleep", sleeper):
            with self.assertRaises(asyncio.CancelledError):
                await self.reporter.run()
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [10, 20, 10])

    async def test_close_cancels_inflight_report(self):
        entered = asyncio.Event()

        async def blocked(client):
            entered.set()
            await asyncio.Event().wait()

        self.reporter.send_once = blocked
        self.reporter.start()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.reporter.close(), 1)
        self.assertIsNone(self.reporter.task)


class HeartbeatConfigurationTests(unittest.TestCase):
    def test_invalid_configuration(self):
        for kwargs in ({"heartbeat_interval": 0}, {"heartbeat_timeout": float("nan")},
                       {"heartbeat_url": "https://controller/heartbeat"},
                       {"heartbeat_url": "http://controller/heartbeat",
                        "advertised_url": "https://worker", "heartbeat_token_path": Path("token")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_lifespan_starts_and_stops_reporting(self):
        settings = Settings(heartbeat_url="https://controller/heartbeat",
                            advertised_url="https://worker", heartbeat_token_path=Path("token"))
        backend = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
        with patch("worker.main.JWTVerifier"), patch("worker.main.HeartbeatReporter") as factory:
            factory.return_value.close = AsyncMock()
            with TestClient(create_app(settings, backend)):
                factory.return_value.start.assert_called_once()
            factory.return_value.close.assert_awaited_once()
            backend.close.assert_awaited_once()