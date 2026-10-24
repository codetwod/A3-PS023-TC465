from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status

from worker.api import WorkerBackend

router = APIRouter(prefix="/vms", tags=["vms"])


def get_backend(request: Request) -> WorkerBackend:
    backend = getattr(request.app.state, "backend", None)
    if backend is None or not getattr(request.app.state, "ready", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VM backend is not ready",
        )
    return backend


Backend = Annotated[WorkerBackend, Depends(get_backend)]
VMId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vm(payload: Annotated[dict[str, Any], Body()], backend: Backend):
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="VM configuration must not be empty",
        )
    return await backend.create_vm(payload)


@router.get("")
async def list_vms(backend: Backend):
    return await backend.list_vms()


@router.post("/{vm_id}/start")
async def start_vm(vm_id: VMId, backend: Backend):
    return await backend.start_vm(vm_id)


@router.post("/{vm_id}/stop")
async def stop_vm(vm_id: VMId, backend: Backend):
    return await backend.stop_vm(vm_id)