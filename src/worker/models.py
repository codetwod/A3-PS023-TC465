"""The deliberately small VM configuration supported by the worker."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, FilePath

VM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class VMCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vm_id: Annotated[str, Field(min_length=1, max_length=64, pattern=VM_ID_PATTERN)]
    cpus: Annotated[int, Field(strict=True, ge=1, le=255)] = 1
    memory_mib: Annotated[int, Field(strict=True, ge=128)] = 512
    kernel_path: FilePath
    disk_path: FilePath
    cmdline: Annotated[str, Field(max_length=4096)] = "console=ttyS0 root=/dev/vda rw"

    def hypervisor_config(self) -> dict:
        return {
            "cpus": {"boot_vcpus": self.cpus, "max_vcpus": self.cpus},
            "memory": {"size": self.memory_mib * 1024 * 1024},
            "payload": {"kernel": str(self.kernel_path.resolve()), "cmdline": self.cmdline},
            "disks": [{"path": str(self.disk_path.resolve())}],
            "serial": {"mode": "Null"},
            "console": {"mode": "Off"},
        }
