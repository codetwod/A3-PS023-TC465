"""Basic worker health and resource endpoints."""
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/resources")
async def resources():
    # Resource reporting logic here
    raise HTTPException(status_code=501, detail="Resource reporting is not implemented yet")
