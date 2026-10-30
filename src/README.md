# Cloud Hypervisor worker - part 1

One Linux worker exposes VM create, start, stop, list, health, and memory capacity
over HTTP. It launches one Cloud Hypervisor process per active VM and communicates
over a private Unix socket. API reference:
https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/vmm/src/api/openapi/cloud-hypervisor.yaml

## Linux setup

Requires Python 3.11+, Linux/KVM, Cloud Hypervisor on PATH, and read/write access
to `/dev/kvm`. Supply a compatible guest kernel and a **raw** disk image with a
root filesystem compatible with your command line. Images are operator-provided.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
python -m worker
```

The API listens on `127.0.0.1:8000`; documentation is at `/docs`.
Run exactly **one** server process, without automatic reload. The runtime directory
is locked to prevent two workers from using it at once.

| Environment variable | Default |
| --- | --- |
| `WORKER_HOST` | `127.0.0.1` |
| `WORKER_PORT` | `8000` |
| `WORKER_HYPERVISOR_BINARY` | `cloud-hypervisor` |
| `WORKER_RUNTIME_DIR` | `/tmp/ch-worker` |
| `WORKER_HOST_RESERVE_MIB` | `512` |
| `WORKER_OPERATION_TIMEOUT` | `10` seconds |

Use a private runtime directory owned by the worker user with mode `0700`.
Keep its path short because Linux Unix sockets have a pathname length limit.

## Try the lifecycle

Replace the image paths with files on the **worker host**:

```bash
curl -f -X POST http://127.0.0.1:8000/vms \
  -H 'Content-Type: application/json' \
  -d '{"vm_id":"demo","cpus":1,"memory_mib":512,"kernel_path":"/srv/vms/vmlinux","disk_path":"/srv/vms/demo.raw","cmdline":"console=ttyS0 root=/dev/vda rw"}'
curl -f -X POST http://127.0.0.1:8000/vms/demo/start
curl -f http://127.0.0.1:8000/vms
curl -f http://127.0.0.1:8000/resources
curl -f -X POST http://127.0.0.1:8000/vms/demo/stop
```

- Create validates configuration, checks capacity, launches the VMM, and creates
  the guest without booting it. Created and running VMs both reserve memory.
- Start boots a created guest. Starting a stopped guest launches a new VMM using
  its original configuration. Starting an already running guest succeeds unchanged.
- Stop shuts down the VMM and guest, with process termination/kill as a fallback.
  **This is a hard stop, not a guest OS graceful shutdown.** Shut down the guest
  OS first when filesystem consistency matters. Stop releases memory and retains
  the in-memory configuration for another start.
- List queries current state. An unreachable API is `unknown` and retains its
  reservation; an exited process is `failed` and releases memory.
- Capacity is `max(0, total - host reserve - committed guest memory)`, in MiB.
  This is an admission budget, not an OS memory guarantee; allow reserve for
  other workloads and VMM overhead. Lifecycle and admission updates are serialized.
- A writable disk cannot be attached to two active VMs managed by this worker.
- Health returns 200 after initialization; unavailable backends use 503.
- Hypervisor output is appended to `<runtime directory>/<vm_id>.log`.

## Part 1 limits / part 2 work

VM records live in memory. Normal worker shutdown stops all managed VMs; restarting
loses their configurations. After a crash, existing sockets cause startup to fail.
Inspect and stop surviving VMMs before manually removing stale sockets. Do not
delete a socket for a live VM.

Part 2 adds durable records/restart reconciliation, authenticated worker access,
controller registration/heartbeats, systemd deployment, and real Linux/KVM testing.
Guest networking, console access, image provisioning, and VM deletion are not
provided in part 1. IDs remain reserved until the worker exits.

The API accepts local image paths and has no authentication yet. Keep its default
loopback binding for trusted local use.

## Portable tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests use fake processes and mocked hypervisor HTTP responses. They cover lifecycle,
memory accounting, concurrent admission, disk conflicts, failure cleanup, request
validation, API errors, and readiness. They do not prove a real guest boots.
