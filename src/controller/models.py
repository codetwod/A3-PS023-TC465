from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from worker.models import VM_ID_PATTERN


class VMCreate(BaseModel):
    # Paths belong to the selected worker, not the controller filesystem.
    model_config = ConfigDict(extra="forbid")
    vm_id: Annotated[str, Field(min_length=1, max_length=64, pattern=VM_ID_PATTERN)]
    cpus: Annotated[int, Field(strict=True, ge=1, le=255)] = 1
    memory_mib: Annotated[int, Field(strict=True, ge=128)] = 512
    kernel_path: Annotated[str, Field(min_length=1)]
    disk_path: Annotated[str, Field(min_length=1)]
    cmdline: Annotated[str, Field(max_length=4096)] = "console=ttyS0 root=/dev/vda rw"


class Resources(BaseModel):
    total_memory_mib: Annotated[int, Field(strict=True, ge=0)]
    host_reserve_mib: Annotated[int, Field(strict=True, ge=0)]
    committed_memory_mib: Annotated[int, Field(strict=True, ge=0)]
    allocatable_memory_mib: Annotated[int, Field(strict=True, ge=0)]


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str
    worker_url: str
    status: Literal["ready"]
    sent_at: datetime
    resources: Resources
