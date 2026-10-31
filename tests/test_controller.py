import asyncio
from contextlib import asynccontextmanager
import copy
import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx

from controller.config import Settings
from controller.main import create_app
from controller.models import VMCreate
from controller.service import Controller
from controller.store import Store


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def mutation(self):
        async with self.lock:
            yield

    async def get(self, key):
        return copy.deepcopy(self.records.get(key))

    async def put(self, record):
        self.records[record["vm_id"]] = copy.deepcopy(record)

    async def all(self):
        return copy.deepcopy(list(self.records.values()))

    async def heartbeats(self):
        return {}


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.sent = []
        self.inventories = {"one": [], "two": []}
        self.timeout = False
        self.down = set()
        self.settings = Settings(workers={"one": "http://one", "two": "http://two"})

        def handler(request):
            worker = request.url.host
            self.sent.append((worker, request.method, request.url.path))
            if worker in self.down:
                return httpx.Response(503)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/resources":
                return httpx.Response(200, json={"total_memory_mib": 4096,
                    "host_reserve_mib": 512, "committed_memory_mib": 0,
                    "allocatable_memory_mib": 1024 if worker == "one" else 2048})
            if request.method == "GET":
                return httpx.Response(200, json=self.inventories[worker])
            if self.timeout:
                raise httpx.ReadTimeout("lost reply", request=request)
            import json
            if request.url.path == "/vms":
                value = {**json.loads(request.content), "state": "created"}
                self.inventories[worker].append(value)
            else:
                value = {**self.inventories[worker][0], "state": "running" if request.url.path.endswith("start") else "stopped"}
                self.inventories[worker][0] = value
            return httpx.Response(200, json=value)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.client.aclose)
        self.service = Controller(self.settings, self.store, self.client)
        self.payload = VMCreate(vm_id="demo", kernel_path="/remote/kernel", disk_path="/remote/disk")

    async def test_placement_details_and_lifecycle(self):
        result = await self.service.create(self.payload)
        self.assertEqual(result["worker_id"], "two")
        self.assertEqual(result["state"], "created")
        self.assertEqual({w for w, _, p in self.sent if p == "/health"}, {"one", "two"})
        self.assertEqual((await self.service.action("demo", "start"))["state"], "running")
        self.assertEqual((await self.service.details("demo"))["state"], "running")
        self.assertEqual((await self.service.action("demo", "stop"))["state"], "stopped")

    async def test_concurrent_duplicate_has_one_remote_create(self):
        results = await asyncio.gather(self.service.create(self.payload), self.service.create(self.payload), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, HTTPException) for value in results), 1)
        self.assertEqual(sum(method == "POST" and path == "/vms" for _, method, path in self.sent), 1)

    async def test_timeout_retains_assignment_without_failover(self):
        self.timeout = True
        with self.assertRaises(HTTPException) as error:
            await self.service.create(self.payload)
        self.assertEqual(error.exception.status_code, 504)
        self.assertEqual((await self.store.get("demo"))["state"], "unknown")
        self.assertEqual([w for w, m, p in self.sent if m == "POST"], ["two"])
        self.timeout = False
        self.inventories["two"] = [{"vm_id": "demo", "state": "created"}]
        self.assertEqual((await self.service.reconcile())[0]["state"], "created")

    async def test_unavailable_and_insufficient_workers(self):
        self.down.add("two")
        self.assertEqual((await self.service.create(self.payload))["worker_id"], "one")
        large = self.payload.model_copy(update={"vm_id": "large", "memory_mib": 8192})
        with self.assertRaises(HTTPException) as error:
            await self.service.create(large)
        self.assertEqual(error.exception.status_code, 503)
        self.assertIsNone(await self.store.get("large"))

    async def test_reconciliation_marks_missing_and_unreachable(self):
        await self.service.create(self.payload)
        self.inventories["two"] = []
        self.assertEqual((await self.service.reconcile())[0]["state"], "missing")
        self.down.add("two")
        self.assertEqual((await self.service.reconcile())[0]["state"], "unknown")

    async def test_rejected_create_retains_record_and_does_not_fail_over(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(409) if request.method == "POST" else
                httpx.Response(200, json={"status": "ok"} if request.url.path == "/health" else {
                    "total_memory_mib": 4096, "host_reserve_mib": 0,
                    "committed_memory_mib": 0, "allocatable_memory_mib": 4096}))) as client:
            service = Controller(self.settings, self.store, client)
            with self.assertRaises(HTTPException) as error:
                await service.create(self.payload)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual((await self.store.get("demo"))["state"], "unknown")
            with self.assertRaises(HTTPException):
                await service.create(self.payload)


class FakeRedis:
    def __init__(self):
        self.hashes = {}

    async def ping(self):
        return True

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()

    def lock(self, *args, **kwargs):
        class Lock:
            async def acquire(self):
                return True

            async def release(self):
                pass

        return Lock()


class APITests(unittest.TestCase):
    def test_heartbeat_registration_boundary_and_receive_time(self):
        database = FakeRedis()
        settings = Settings(workers={"one": "http://one"})
        app = create_app(settings, redis=database)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            payload = {"worker_id": "one", "worker_url": "http://one", "status": "ready",
                       "sent_at": "2000-01-01T00:00:00Z", "resources": {
                           "total_memory_mib": 4096, "host_reserve_mib": 512,
                           "committed_memory_mib": 0, "allocatable_memory_mib": 3584}}
            self.assertEqual(client.post("/workers/one/heartbeat", json=payload).status_code, 204)
            self.assertIn("received_at", database.hashes["controller:heartbeats"]["one"])
            self.assertEqual(client.post("/workers/other/heartbeat", json=payload).status_code, 404)
            payload["worker_url"] = "http://untrusted"
            self.assertEqual(client.post("/workers/one/heartbeat", json=payload).status_code, 422)
            self.assertEqual(client.get("/vms/absent").status_code, 404)

    def test_store_roundtrip(self):
        async def run():
            store = Store(FakeRedis(), "test:")
            await store.put({"vm_id": "demo", "state": "unknown"})
            self.assertEqual((await store.get("demo"))["state"], "unknown")
            self.assertEqual(len(await store.all()), 1)
        asyncio.run(run())
