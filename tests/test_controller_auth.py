import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from cryptography.hazmat.primitives import serialization

import httpx
import jwt
from fastapi.testclient import TestClient

from controller.auth import Auth
from controller.config import Settings
from controller.main import create_app
from controller_support import auth_files
from test_controller import FakeRedis
from worker.config import Settings as WorkerSettings
from worker.main import create_app as worker_app
from worker.heartbeat import HeartbeatReporter


class ControllerAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        config, self.key, self.heartbeat_key, self.public_path = auth_files(self.directory)
        self.settings = Settings(workers={"worker-1": "http://127.0.0.1:8001"}, **config)
        self.redis = FakeRedis()
        self.backend = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock(),
            resources=AsyncMock(return_value={"total_memory_mib": 4096, "host_reserve_mib": 512,
                "committed_memory_mib": 0, "allocatable_memory_mib": 3584}),
            list_vms=AsyncMock(return_value=[]), create_vm=AsyncMock(),
            start_vm=AsyncMock(), stop_vm=AsyncMock())
        self.worker = worker_app(WorkerSettings(jwt_public_key_path=self.public_path,
                                               jwt_audience="worker-1"), self.backend)
        # Real worker API and JWT verification; only hypervisor backend is mocked.
        self.enterContext(TestClient(self.worker))
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.worker))
        self.app = create_app(self.settings, redis=self.redis, client=http)
        self.client = self.enterContext(TestClient(self.app))
        # Client is injected; close it on the same portal before TestClient exits.
        self.addCleanup(lambda: self.client.portal.call(http.aclose) if self.client.portal else None)

    def login(self, name="alice"):
        response = self.client.post("/auth/login", json={"username": name, "password": "test-password-123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_authenticated_lifecycle_and_owner_isolation(self):
        alice, bob, admin = self.login(), self.login("bob"), self.login("admin")
        kernel, disk = Path(self.directory) / "kernel", Path(self.directory) / "disk"
        kernel.touch()
        disk.touch()
        payload = {"vm_id": "demo", "kernel_path": str(kernel), "disk_path": str(disk)}
        self.backend.create_vm.return_value = {**payload, "state": "created"}
        response = self.client.post("/vms", json=payload, headers=alice)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["owner_id"], "alice")
        self.backend.list_vms.return_value = [{**payload, "state": "created"}]
        self.assertEqual(self.client.get("/vms", headers=bob).json(), [])
        for method, path in (("GET", "/vms/demo"), ("POST", "/vms/demo/start"), ("POST", "/vms/demo/stop")):
            self.assertEqual(self.client.request(method, path, headers=bob).status_code, 404)
        self.backend.start_vm.assert_not_awaited()
        self.assertEqual(self.client.get("/vms/demo", headers=alice).status_code, 200)
        self.backend.start_vm.return_value = {**payload, "state": "running"}
        self.backend.stop_vm.return_value = {**payload, "state": "stopped"}
        self.assertEqual(self.client.post("/vms/demo/start", headers=alice).json()["state"], "running")
        self.assertEqual(self.client.post("/vms/demo/stop", headers=admin).json()["state"], "stopped")
        self.assertEqual(self.client.get("/workers", headers=alice).status_code, 403)
        self.assertEqual(self.client.get("/workers", headers=admin).status_code, 200)
        self.assertEqual(self.client.post("/vms", json=payload, headers=bob).status_code, 409)

    def test_routes_require_authentication(self):
        for method, path in (("GET", "/vms"), ("POST", "/vms"), ("GET", "/vms/demo"),
                             ("POST", "/vms/demo/start"), ("POST", "/vms/demo/stop"),
                             ("GET", "/workers"), ("GET", "/auth/me"),
                             ("POST", "/workers/worker-1/heartbeat")):
            self.assertEqual(self.client.request(method, path).status_code, 401, path)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_bad_password_and_login_throttle(self):
        for i in range(10):
            self.assertEqual(self.client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code, 429)

    def test_invalid_jwt_claims_and_signature(self):
        stamp = int(time.time())
        base = {"iss": "vm-controller", "aud": "vm-controller-api", "sub": "alice",
                "iat": stamp - 1, "exp": stamp + 300, "token_type": "user"}
        for changes in ({"exp": stamp - 10}, {"iat": stamp + 1000}, {"nbf": stamp + 1000},
                        {"iss": "wrong"}, {"aud": "worker-1"}, {"sub": "unknown"},
                        {"exp": True}, {"token_type": "worker"}):
            token = jwt.encode({**base, **changes}, self.key, algorithm="RS256")
            self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + token}).status_code, 401)
        for name in ("iss", "aud", "sub", "iat", "exp"):
            token = jwt.encode({k: v for k, v in base.items() if k != name}, self.key, algorithm="RS256")
            self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + token}).status_code, 401)
        token = jwt.encode(base, self.heartbeat_key, algorithm="RS256")
        self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + token}).status_code, 401)

    def test_heartbeat_token_isolation(self):
        payload = {"worker_id": "worker-1", "worker_url": "http://127.0.0.1:8001", "status": "ready",
                   "sent_at": "2026-01-01T00:00:00Z", "resources": self.backend.resources.return_value}
        self.assertEqual(self.client.post("/workers/worker-1/heartbeat", json=payload, headers=self.login("admin")).status_code, 401)
        stamp = int(time.time())
        claims = {"iss": "worker-heartbeats", "aud": "vm-controller-heartbeats",
                  "sub": "worker-1", "iat": stamp, "exp": stamp + 300}
        headers = {"Authorization": "Bearer " + jwt.encode(claims, self.heartbeat_key, algorithm="RS256")}
        self.assertEqual(self.client.post("/workers/worker-1/heartbeat", json=payload, headers=headers).status_code, 204)
        claims["sub"] = "worker-2"
        headers = {"Authorization": "Bearer " + jwt.encode(claims, self.heartbeat_key, algorithm="RS256")}
        self.assertEqual(self.client.post("/workers/worker-1/heartbeat", json=payload, headers=headers).status_code, 401)

    def test_worker_reporter_automatically_signs_accepted_heartbeat(self):
        path = Path(self.directory) / "heartbeat-private.pem"
        path.write_bytes(self.heartbeat_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        settings = WorkerSettings(worker_id="worker-1", advertised_url="http://127.0.0.1:8001",
            heartbeat_url="http://127.0.0.1:9000/workers/worker-1/heartbeat",
            heartbeat_private_key_path=path)

        async def send():
            reporter = HeartbeatReporter(settings, self.backend)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                await reporter.send_once(client)
                await reporter.send_once(client)
        self.client.portal.call(send)
        report = json.loads(self.redis.hashes["controller:heartbeats"]["worker-1"])
        self.assertEqual(report["worker_id"], "worker-1")
        self.assertIn("received_at", report)

    def test_legacy_records_are_admin_only(self):
        self.redis.hashes["controller:vms"] = {"legacy": json.dumps({"vm_id": "legacy",
            "worker_id": "worker-1", "state": "stopped"})}
        self.assertEqual(self.client.get("/vms/legacy", headers=self.login()).status_code, 404)
        self.assertEqual(self.client.get("/vms/legacy", headers=self.login("admin")).status_code, 200)

    def test_per_worker_keys_reject_forged_other_worker_identity(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        timestamp = int(time.time())
        foreign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({"iss": "worker-heartbeats", "aud": "vm-controller-heartbeats",
            "sub": "worker-1", "iat": timestamp, "exp": timestamp + 300}, foreign_key, algorithm="RS256")
        payload = {"worker_id": "worker-1", "worker_url": "http://127.0.0.1:8001", "status": "ready",
                   "sent_at": "2026-01-01T00:00:00Z", "resources": self.backend.resources.return_value}
        self.assertEqual(self.client.post("/workers/worker-1/heartbeat", json=payload,
            headers={"Authorization": "Bearer " + token}).status_code, 401)

    def test_startup_requires_auth_configuration(self):
        with self.assertRaises(ValueError):
            Auth(Settings())
