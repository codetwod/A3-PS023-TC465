from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from worker.api import WorkerBackend, WorkerOperationError
from worker.api.health import router as health_router
from worker.api.vms import router as vm_router
from worker.config import Settings
from worker.backend import CloudHypervisorBackend

def create_app(settings: Settings | None = None, backend: WorkerBackend | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ready = False
        application.state.settings = settings if settings is not None else Settings.from_env()
        active_backend = backend if backend is not None else CloudHypervisorBackend(application.state.settings)
        application.state.backend = active_backend
        try:
            await active_backend.initialize()
            application.state.ready = True
            yield
        finally:
            application.state.ready = False
            await active_backend.close()

    application = FastAPI(title="Cloud Hypervisor Worker", version="0.1.0", lifespan=lifespan)
    application.state.ready = False
    application.state.backend = backend

    @application.exception_handler(WorkerOperationError)
    async def operation_error(request: Request, exc: WorkerOperationError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    application.include_router(health_router)
    application.include_router(vm_router)
    return application

app = create_app()
