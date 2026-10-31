import asyncio
from contextlib import asynccontextmanager, suppress
import json

from fastapi import HTTPException


class Store:
    def __init__(self, redis, prefix):
        self.redis = redis
        self.prefix = prefix

    async def get(self, vm_id):
        value = await self.redis.hget(self.prefix + "vms", vm_id)
        return json.loads(value) if value else None

    async def put(self, record):
        await self.redis.hset(self.prefix + "vms", record["vm_id"], json.dumps(record))

    async def all(self):
        return [json.loads(value) for value in (await self.redis.hgetall(self.prefix + "vms")).values()]

    async def heartbeat(self, worker_id, report):
        await self.redis.hset(self.prefix + "heartbeats", worker_id, json.dumps(report))

    async def heartbeats(self):
        return {key: json.loads(value) for key, value in
                (await self.redis.hgetall(self.prefix + "heartbeats")).items()}

    @asynccontextmanager
    async def mutation(self):
        # Cross-process serialization includes remote admission and reconciliation.
        lock = self.redis.lock(self.prefix + "mutation", timeout=30, blocking_timeout=5)
        if not await lock.acquire():
            raise HTTPException(503, "Controller is busy; retry later")
        owner = asyncio.current_task()

        async def renew():
            try:
                while True:
                    await asyncio.sleep(10)
                    await lock.extend(30, replace_ttl=True)
            except Exception:
                owner.cancel()  # Never continue mutations after losing the lease.
                raise

        task = asyncio.create_task(renew())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await lock.release()
