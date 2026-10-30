from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status

from worker.api import WorkerBackend
from worker.models import VMCreate, VM_ID_PATTERN
from worker.auth import require_controller

router = APIRouter(prefix="/vms", tags=["vms"], dependencies=[Depends(require_controller)])


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
    Path(min_length=1, max_length=64, pattern=VM_ID_PATTERN),
]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vm(payload: Annotated[VMCreate, Body()], backend: Backend):
    return await backend.create_vm(payload.model_dump(mode="json"))


@router.get("")
async def list_vms(backend: Backend):
    return await backend.list_vms()


@router.post("/{vm_id}/start")
async def start_vm(vm_id: VMId, backend: Backend):
    return await backend.start_vm(vm_id)


@router.post("/{vm_id}/stop")
async def stop_vm(vm_id: VMId, backend: Backend):
    return await backend.stop_vm(vm_id)