import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
import httpx

from controller.models import Resources


def now():
    return datetime.now(timezone.utc).isoformat()


class Controller:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client

    async def request(self, worker_id, method, path, **kwargs):
        headers = {}
        token_path = self.settings.worker_token_paths.get(worker_id)
        if token_path:
            try:
                token = Path(token_path).read_text().strip()
                if not token:
                    raise ValueError("empty token")
                headers["Authorization"] = "Bearer " + token
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "Worker credential unavailable") from exc
        try:
            response = await self.client.request(
                method, self.settings.workers[worker_id].rstrip("/") + path,
                headers=headers, timeout=self.settings.request_timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "Worker request timed out; outcome may be unknown") from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, "Worker is unreachable; outcome may be unknown") from exc
        if not response.is_success:
            raise HTTPException(response.status_code if response.status_code in (404, 409, 422) else 502,
                                f"Worker rejected request (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(502, "Invalid worker response") from exc

    async def probe(self, worker_id):
        try:
            await self.request(worker_id, "GET", "/health")
            resources = Resources.model_validate(await self.request(worker_id, "GET", "/resources"))
            return worker_id, resources.model_dump()
        except (HTTPException, ValueError):
            return worker_id, None

    async def workers(self):
        reports = await self.store.heartbeats()
        probes = await asyncio.gather(*(self.probe(key) for key in self.settings.workers))
        result = []
        for key, resources in probes:
            report = reports.get(key)
            stale = report is None or (datetime.now(timezone.utc) -
                    datetime.fromisoformat(report["received_at"])).total_seconds() > self.settings.heartbeat_stale_seconds
            result.append({"worker_id": key, "worker_url": self.settings.workers[key],
                           "healthy": resources is not None, "resources": resources,
                           "heartbeat_stale": stale, "heartbeat": report})
        return result

    async def create(self, payload):
        async with self.store.mutation():
            if await self.store.get(payload.vm_id):
                raise HTTPException(409, "VM ID already exists")
            candidates = [worker for worker in await self.workers()
                          if worker["healthy"] and worker["resources"]["allocatable_memory_mib"] >= payload.memory_mib]
            if not candidates:
                raise HTTPException(503, "No healthy worker has sufficient allocatable memory")
            candidates.sort(key=lambda worker: (-worker["resources"]["allocatable_memory_mib"], worker["worker_id"]))
            record = {"vm_id": payload.vm_id, "worker_id": candidates[0]["worker_id"],
                      "config": payload.model_dump(), "state": "creating", "created_at": now(),
                      "updated_at": now(), "worker_vm": None, "last_error": None}
            # Persist assignment before sending: uncertain creates must never fail over.
            await self.store.put(record)
            return await self.perform(record, "POST", "/vms", json=record["config"])

    async def perform(self, record, method, path, **kwargs):
        try:
            result = await self.request(record["worker_id"], method, path, **kwargs)
            if not isinstance(result, dict) or result.get("vm_id") != record["vm_id"] or not isinstance(result.get("state"), str):
                raise HTTPException(502, "Invalid worker VM response")
        except HTTPException as exc:
            record.update(state="unknown", last_error=exc.detail, updated_at=now())
            await self.store.put(record)
            raise
        record.update(state=result["state"], worker_vm=result, last_error=None, updated_at=now())
        await self.store.put(record)
        return record

    async def get_record(self, vm_id):
        record = await self.store.get(vm_id)
        if record is None:
            raise HTTPException(404, "VM not found")
        return record

    async def action(self, vm_id, action):
        async with self.store.mutation():
            record = await self.get_record(vm_id)
            record.update(state={"start": "starting", "stop": "stopping"}[action], updated_at=now())
            await self.store.put(record)
            return await self.perform(record, "POST", f"/vms/{vm_id}/{action}")

    async def refresh(self, records):
        async def inventory(worker_id):
            try:
                values = await self.request(worker_id, "GET", "/vms")
                if not isinstance(values, list) or any(not isinstance(v, dict) or
                        not isinstance(v.get("vm_id"), str) or not isinstance(v.get("state"), str) for v in values):
                    raise HTTPException(502, "Invalid worker inventory")
                return worker_id, {value["vm_id"]: value for value in values}, None
            except HTTPException as exc:
                return worker_id, {}, exc.detail

        inventories = {key: (values, error) for key, values, error in await asyncio.gather(
            *(inventory(key) for key in {r["worker_id"] for r in records} if key in self.settings.workers))}
        for record in records:
            values, error = inventories.get(record["worker_id"], ({}, "Assigned worker is no longer configured"))
            value = values.get(record["vm_id"])
            record.update(state="unknown" if error else value["state"] if value else "missing",
                          last_error=error or (None if value else "VM absent from assigned worker"), updated_at=now())
            if value:
                record["worker_vm"] = value
            await self.store.put(record)
        return records

    async def details(self, vm_id):
        async with self.store.mutation():
            return (await self.refresh([await self.get_record(vm_id)]))[0]

    async def reconcile(self):
        async with self.store.mutation():
            return await self.refresh(await self.store.all())
