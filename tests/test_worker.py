"""Portable API/lifecycle checks; no KVM or real guest is used here."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from worker.api import WorkerOperationError
from worker.backend import CloudHypervisorBackend
from worker.config import Settings
from worker.main import create_app


class FakeProcess:
    pid = 123
    returncode = None

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.kernel = self.directory / "vmlinux"
        self.disk = self.directory / "rootfs.raw"
        self.kernel.touch()
        self.disk.touch()
        self.backend = CloudHypervisorBackend(Settings(runtime_dir=self.directory))
        self.payload = dict(vm_id="demo", cpus=2, memory_mib=512,
                            kernel_path=str(self.kernel), disk_path=str(self.disk))
        self.states = {}

        async def request(record, method, operation, payload=None):
            if operation == "vm.create":
                self.states[record.config.vm_id] = "Created"
            if operation == "vm.boot":
                self.states[record.config.vm_id] = "Running"
            if operation == "vm.info":
                return {"state": self.states[record.config.vm_id]}
            return None

        self.request = AsyncMock(side_effect=request)
        self.backend._request = self.request
        self.spawn = patch("worker.backend.asyncio.create_subprocess_exec",
                           side_effect=self._spawn)
        self.spawn_mock = self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.memory = patch("worker.backend.psutil.virtual_memory",
                            return_value=SimpleNamespace(total=2048 * 1024 * 1024))
        self.memory.start()
        self.addCleanup(self.memory.stop)

    async def _spawn(self, *args, **kwargs):
        return FakeProcess()

    async def test_lifecycle_and_reservations(self):
        vm = await self.backend.create_vm(self.payload)
        self.assertEqual(vm["state"], "created")
        self.assertEqual((await self.backend.resources())["allocatable_memory_mib"], 1024)
        self.assertEqual((await self.backend.start_vm("demo"))["state"], "running")
        self.assertEqual((await self.backend.list_vms())[0]["state"], "running")
        await self.backend.start_vm("demo")
        self.assertEqual(self.spawn_mock.call_count, 1)
        self.assertEqual((await self.backend.stop_vm("demo"))["state"], "stopped")
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 0)
        await self.backend.start_vm("demo")
        self.assertEqual(self.spawn_mock.call_count, 2)
        await self.backend.close()
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 0)

    async def test_duplicate_and_shared_disk_rejected(self):
        await self.backend.create_vm(self.payload)
        for payload in (self.payload, {**self.payload, "vm_id": "other"}):
            with self.assertRaises(WorkerOperationError) as error:
                await self.backend.create_vm(payload)
            self.assertEqual(error.exception.status_code, 409)

    async def test_concurrent_admission_does_not_overcommit(self):
        other_disk = self.directory / "other.raw"
        other_disk.touch()
        results = await asyncio.gather(
            self.backend.create_vm({**self.payload, "memory_mib": 1024}),
            self.backend.create_vm({**self.payload, "vm_id": "other", "memory_mib": 1024,
                                    "disk_path": str(other_disk)}), return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 1024)

    async def test_failed_creation_cleans_process_and_reservation(self):
        async def fail(record, method, operation, payload=None):
            if operation == "vm.create":
                raise WorkerOperationError(502, "bad configuration")
        self.request.side_effect = fail
        with self.assertRaises(WorkerOperationError):
            await self.backend.create_vm(self.payload)
        self.assertEqual(await self.backend.list_vms(), [])
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 0)

    async def test_dead_process_and_unreachable_api(self):
        await self.backend.create_vm(self.payload)
        self.request.side_effect = WorkerOperationError(502, "unreachable")
        self.assertEqual((await self.backend.list_vms())[0]["state"], "unknown")
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 512)
        self.backend.records["demo"].process.returncode = 1
        self.assertEqual((await self.backend.list_vms())[0]["state"], "failed")
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 0)

    async def test_unknown_vm(self):
        with self.assertRaises(WorkerOperationError) as error:
            await self.backend.start_vm("missing")
        self.assertEqual(error.exception.status_code, 404)

    async def test_stop_falls_back_when_api_is_unreachable(self):
        await self.backend.create_vm(self.payload)
        process = self.backend.records["demo"].process
        self.request.side_effect = WorkerOperationError(502, "unreachable")
        self.assertEqual((await self.backend.stop_vm("demo"))["state"], "stopped")
        self.assertEqual(process.returncode, -15)
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 0)

    async def test_boot_error_preserves_created_vm(self):
        await self.backend.create_vm(self.payload)
        original = self.request.side_effect

        async def fail_boot(record, method, operation, payload=None):
            if operation == "vm.boot":
                raise WorkerOperationError(502, "boot failed")
            return await original(record, method, operation, payload)

        self.request.side_effect = fail_boot
        with self.assertRaises(WorkerOperationError):
            await self.backend.start_vm("demo")
        self.assertEqual(self.backend.records["demo"].state, "created")
        self.assertEqual((await self.backend.resources())["committed_memory_mib"], 512)

    async def test_hypervisor_payload(self):
        await self.backend.create_vm(self.payload)
        config = next(call.args[3] for call in self.request.call_args_list
                      if call.args[2] == "vm.create")
        self.assertEqual(config["memory"]["size"], 512 * 1024 * 1024)
        self.assertEqual(config["cpus"], {"boot_vcpus": 2, "max_vcpus": 2})
        self.assertEqual(config["payload"]["kernel"], str(self.kernel.resolve()))

    async def test_unix_api_request_and_error_mapping(self):
        await self.backend.create_vm(self.payload)
        record = self.backend.records["demo"]
        observed = []

        def handler(request):
            observed.append(request)
            return httpx.Response(409, text="invalid state")

        transport = httpx.MockTransport(handler)
        with patch("worker.backend.httpx.AsyncHTTPTransport", return_value=transport) as factory:
            with self.assertRaises(WorkerOperationError) as error:
                await CloudHypervisorBackend._request(self.backend, record, "PUT", "vm.boot")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(observed[0].url.path, "/api/v1/vm.boot")
        factory.assert_called_once_with(uds=str(record.socket))


class ApiTests(unittest.TestCase):
    def test_api_validation_and_lifespan(self):
        backend = SimpleNamespace(
            initialize=AsyncMock(), close=AsyncMock(), create_vm=AsyncMock(),
            list_vms=AsyncMock(return_value=[]),
            resources=AsyncMock(return_value={"allocatable_memory_mib": 1024}),
            start_vm=AsyncMock(side_effect=WorkerOperationError(404, "VM not found")),
        )
        app = create_app(Settings(), backend)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/vms").json(), [])
            self.assertEqual(client.get("/resources").status_code, 200)
            self.assertEqual(client.post("/vms", json={}).status_code, 422)
            self.assertEqual(client.post("/vms", json={
                "vm_id": "../escape", "memory_mib": -1, "cpus": True,
                "kernel_path": "/missing/kernel", "disk_path": "/missing/disk",
                "unexpected": "value",
            }).status_code, 422)
            self.assertEqual(client.post("/vms/demo/start").status_code, 404)
            self.assertEqual(client.post("/vms/bad!id/start").status_code, 422)
            backend.create_vm.assert_not_called()
        backend.initialize.assert_awaited_once()
        backend.close.assert_awaited_once()
        self.assertFalse(app.state.ready)

    def test_not_ready(self):
        client = TestClient(create_app())
        self.assertEqual(client.get("/health").status_code, 503)
        self.assertEqual(client.get("/resources").status_code, 503)


if __name__ == "__main__":
    unittest.main()
