from typing import Any, Protocol


class WorkerOperationError(Exception):

    def __init__(self, status_code: int, detail: str):
        if not 400 <= status_code <= 599:
            raise ValueError("Backend error status must be between 400 and 599")
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class WorkerBackend(Protocol):

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create_vm(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def list_vms(self) -> list[dict[str, Any]]: ...

    async def start_vm(self, vm_id: str) -> dict[str, Any]: ...

    async def stop_vm(self, vm_id: str) -> dict[str, Any]: ...

    async def resources(self) -> dict[str, int]: ...
