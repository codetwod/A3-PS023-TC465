from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/vms", tags=["vms"])


@router.post("")
async def create_vm(payload: dict):
    # request validation and VM creation logic
    raise HTTPException(status_code=501, detail="VM creation is not implemented yet")


@router.get("")
async def list_vms():
    raise HTTPException(status_code=501, detail="VM listing is not implemented yet")


@router.post("/{vm_id}/start")
async def start_vm(vm_id: str):
    raise HTTPException(status_code=501, detail="VM start is not implemented yet")


@router.post("/{vm_id}/stop")
async def stop_vm(vm_id: str):
    raise HTTPException(status_code=501, detail="VM stop is not implemented yet")
