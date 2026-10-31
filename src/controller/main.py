import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from controller.config import Settings
from controller.models import Heartbeat, VMCreate
from controller.service import Controller, now
from controller.store import Store
from controller.auth import Auth, Login, require_user, require_admin, require_worker

logger = logging.getLogger(__name__)


def create_app(settings=None, redis=None, client=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        app.state.auth = Auth(config)
        database = redis if redis is not None else Redis.from_url(
            config.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        http = client if client is not None else httpx.AsyncClient(trust_env=False, follow_redirects=False)
        task = None
        try:
            await database.ping()
            service = Controller(config, Store(database, config.redis_prefix), http, app.state.auth)
            app.state.controller = service
            await service.reconcile()

            async def reconcile_loop():
                while True:
                    await asyncio.sleep(config.reconcile_interval)
                    try:
                        await service.reconcile()
                    except Exception:
                        logger.exception("Controller reconciliation failed")

            task = asyncio.create_task(reconcile_loop())
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if client is None:
                await http.aclose()
            if redis is None:
                await database.aclose()

    app = FastAPI(title="VM Controller", lifespan=lifespan)

    @app.exception_handler(RedisError)
    async def redis_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Controller storage unavailable"})

    def service(request):
        return request.app.state.controller

    @app.post("/auth/login")
    async def login(payload: Login, request: Request):
        import hashlib
        controller = service(request)
        # Atomic fixed-window limits shared by all controller processes.
        for value, limit in ((request.client.host if request.client else "unknown", 30),
                             ("user:" + payload.username, 10)):
            key = controller.settings.redis_prefix + "login:" + hashlib.sha256(value.encode()).hexdigest()
            count = await controller.store.redis.eval(
                "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, key)
            if count > limit:
                raise HTTPException(429, "Too many login attempts; retry in one minute", headers={"Retry-After": "60"})
        result = await run_in_threadpool(request.app.state.auth.login, payload)
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/auth/me")
    async def me(user=Depends(require_user)):
        return user

    @app.get("/health")
    async def health(request: Request):
        await service(request).store.redis.ping()
        return {"status": "ok"}

    @app.get("/workers")
    async def workers(request: Request, user=Depends(require_admin)):
        return await service(request).workers()

    @app.post("/workers/{worker_id}/heartbeat", status_code=204)
    async def heartbeat(worker_id: str, payload: Heartbeat, request: Request, claims=Depends(require_worker)):
        controller = service(request)
        if worker_id not in controller.settings.workers:
            raise HTTPException(404, "Worker is not configured")
        if payload.worker_id != worker_id or payload.worker_url.rstrip("/") != controller.settings.workers[worker_id].rstrip("/"):
            raise HTTPException(422, "Heartbeat identity/URL does not match configured worker")
        await controller.store.heartbeat(worker_id, {**payload.model_dump(mode="json"), "received_at": now()})

    @app.post("/vms", status_code=201)
    async def create(payload: VMCreate, request: Request, user=Depends(require_user)):
        return await service(request).create(payload, user)

    @app.get("/vms")
    async def list_vms(request: Request, user=Depends(require_user)):
        return await service(request).reconcile(user)

    @app.get("/vms/{vm_id}")
    async def details(vm_id: str, request: Request, user=Depends(require_user)):
        return await service(request).details(vm_id, user)

    @app.post("/vms/{vm_id}/start")
    async def start(vm_id: str, request: Request, user=Depends(require_user)):
        return await service(request).action(vm_id, "start", user)

    @app.post("/vms/{vm_id}/stop")
    async def stop(vm_id: str, request: Request, user=Depends(require_user)):
        return await service(request).action(vm_id, "stop", user)

    return app


app = create_app()
