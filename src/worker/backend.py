"""Single-process Linux worker using Cloud Hypervisor's Unix-socket HTTP API."""
import asyncio
import logging
import os
from pathlib import Path
import shutil
import sys
from dataclasses import dataclass
from typing import Any

import httpx
import psutil

from worker.api import WorkerOperationError
from worker.config import Settings
from worker.models import VMCreate

logger = logging.getLogger(__name__)


@dataclass
class VMRecord:
    config: VMCreate
    socket: Path
    state: str = "stopped"
    process: asyncio.subprocess.Process | None = None

    def view(self) -> dict[str, Any]:
        return {
            **self.config.model_dump(mode="json"),
            "state": self.state,
            "pid": self.process.pid if self.process and self.process.returncode is None else None,
        }


class CloudHypervisorBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.records: dict[str, VMRecord] = {}
        # Serialize admission and lifecycle changes to avoid double allocation.
        self.lock = asyncio.Lock()
        self._directory_lock = None

    async def initialize(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("The worker requires Linux with KVM support")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise RuntimeError("/dev/kvm must exist and be readable/writable by the worker")
        if shutil.which(self.settings.hypervisor_binary) is None:
            raise RuntimeError("Cloud Hypervisor executable was not found")
        directory = self.settings.runtime_dir.resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise RuntimeError("Worker runtime directory must be owned by this user with mode 0700")
        # Linux-only import keeps API tests importable on other platforms.
        import fcntl
        self._directory_lock = (directory / "worker.lock").open("a")
        try:
            fcntl.flock(self._directory_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if list(directory.glob("*.sock")):
                raise RuntimeError("Existing VM sockets found; restart recovery is not implemented. "
                                   "Inspect existing hypervisor processes before removing stale sockets.")
        except BaseException:
            self._directory_lock.close()
            self._directory_lock = None
            raise

    async def close(self) -> None:
        async with self.lock:
            results = await asyncio.gather(
                *(self._dispose(record) for record in self.records.values()),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("VM cleanup failed: %s", result)
            if self._directory_lock is not None:
                self._directory_lock.close()
                self._directory_lock = None

    def _resources(self) -> dict[str, int]:
        total = psutil.virtual_memory().total // (1024 * 1024)
        committed = sum(
            record.config.memory_mib for record in self.records.values()
            if record.process is not None and record.process.returncode is None
        )
        return {
            "total_memory_mib": total,
            "host_reserve_mib": self.settings.host_reserve_mib,
            "committed_memory_mib": committed,
            "allocatable_memory_mib": max(0, total - self.settings.host_reserve_mib - committed),
        }

    async def resources(self) -> dict[str, int]:
        async with self.lock:
            return self._resources()

    def _admit(self, config: VMCreate) -> None:
        if config.memory_mib > self._resources()["allocatable_memory_mib"]:
            raise WorkerOperationError(409, "Insufficient allocatable memory")
        # A writable guest disk must not be attached to two managed VMs.
        for record in self.records.values():
            if (record.process is not None and record.process.returncode is None
                    and record.config.disk_path.resolve() == config.disk_path.resolve()):
                raise WorkerOperationError(409, "Disk is already attached to an active VM")

    async def _request(self, record: VMRecord, method: str, operation: str,
                       payload: dict | None = None) -> Any:
        transport = httpx.AsyncHTTPTransport(uds=str(record.socket))
        try:
            async with httpx.AsyncClient(transport=transport,
                                        timeout=self.settings.operation_timeout) as client:
                response = await client.request(
                    method, f"http://localhost/api/v1/{operation}", json=payload,
                )
        except httpx.TimeoutException as exc:
            raise WorkerOperationError(504, f"Hypervisor timed out during {operation}") from exc
        except httpx.RequestError as exc:
            raise WorkerOperationError(502, f"Cannot reach hypervisor during {operation}") from exc
        if not response.is_success:
            code = 409 if response.status_code == 409 else 502
            raise WorkerOperationError(code, f"Hypervisor rejected {operation}: {response.text[:500]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise WorkerOperationError(502, "Hypervisor returned invalid JSON") from exc

    async def _prepare(self, record: VMRecord) -> None:
        self._admit(record.config)
        # Revalidate files: a stopped VM's images might have been removed.
        for path in (record.config.kernel_path, record.config.disk_path):
            if not path.is_file():
                raise WorkerOperationError(422, f"Image file does not exist: {path}")
        if len(os.fsencode(record.socket)) > 100:
            raise WorkerOperationError(422, "Runtime directory is too long for a Unix socket")
        log_path = self.settings.runtime_dir / f"{record.config.vm_id}.log"
        try:
            with log_path.open("ab") as log:
                record.process = await asyncio.create_subprocess_exec(
                    self.settings.hypervisor_binary, "--api-socket", str(record.socket),
                    stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                )
            async with asyncio.timeout(self.settings.operation_timeout):
                while True:
                    if record.process.returncode is not None:
                        raise WorkerOperationError(502, f"Hypervisor exited; see {log_path}")
                    try:
                        await self._request(record, "GET", "vmm.ping")
                        break
                    except WorkerOperationError as exc:
                        if exc.status_code not in (502, 504):
                            raise
                        await asyncio.sleep(0.05)
            await self._request(record, "PUT", "vm.create", record.config.hypervisor_config())
            record.state = "created"
        except BaseException as exc:
            await asyncio.shield(self._dispose(record))
            if isinstance(exc, TimeoutError):
                raise WorkerOperationError(504, "Timed out waiting for hypervisor startup") from exc
            if isinstance(exc, OSError):
                raise WorkerOperationError(502, f"Could not launch hypervisor: {exc}") from exc
            raise

    async def _dispose(self, record: VMRecord) -> None:
        process = record.process
        if process is not None and process.returncode is None:
            try:
                await self._request(record, "PUT", "vmm.shutdown")
            except WorkerOperationError:
                logger.warning("VMM shutdown failed for %s; terminating process", record.config.vm_id)
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), self.settings.operation_timeout)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        record.process = None
        record.socket.unlink(missing_ok=True)
        record.state = "stopped"

    def _get(self, vm_id: str) -> VMRecord:
        if vm_id not in self.records:
            raise WorkerOperationError(404, "VM not found")
        return self.records[vm_id]

    async def create_vm(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = VMCreate.model_validate(payload)
        async with self.lock:
            if config.vm_id in self.records:
                raise WorkerOperationError(409, "VM ID already exists")
            record = VMRecord(config, self.settings.runtime_dir.resolve() / f"{config.vm_id}.sock")
            await self._prepare(record)
            self.records[config.vm_id] = record
            return record.view()

    async def _refresh(self, record: VMRecord) -> None:
        if record.process is None:
            return
        if record.process.returncode is not None:
            record.state = "failed"
            return
        try:
            info = await self._request(record, "GET", "vm.info")
            record.state = info["state"].lower()
        except (WorkerOperationError, TypeError, KeyError, AttributeError):
            # Keep the reservation while the process is alive, even if its API fails.
            record.state = "unknown"

    async def list_vms(self) -> list[dict[str, Any]]:
        async with self.lock:
            for record in self.records.values():
                await self._refresh(record)
            return [record.view() for record in self.records.values()]

    async def start_vm(self, vm_id: str) -> dict[str, Any]:
        async with self.lock:
            record = self._get(vm_id)
            await self._refresh(record)
            if record.state == "running":
                return record.view()
            if record.state in ("stopped", "failed"):
                await self._dispose(record)
                await self._prepare(record)
            if record.state != "created":
                raise WorkerOperationError(409, "VM must be stopped before it can be started")
            await self._request(record, "PUT", "vm.boot")
            record.state = "running"
            return record.view()

    async def stop_vm(self, vm_id: str) -> dict[str, Any]:
        async with self.lock:
            record = self._get(vm_id)
            await self._dispose(record)
            return record.view()