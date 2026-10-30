from fastapi import APIRouter, Request, Depends
from worker.auth import require_controller
from fastapi.responses import JSONResponse
from worker.api.vms import Backend

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    ready = getattr(request.app.state, "ready", False)
    return JSONResponse(status_code=200 if ready else 503,
                        content={"status": "ok" if ready else "not_ready"})


@router.get("/resources", dependencies=[Depends(require_controller)])
async def resources(backend: Backend):
    return await backend.resources()