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
# First configure JWT verification using the authentication instructions below.
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

Part 2 adds durable records/restart reconciliation,
controller registration/heartbeats, systemd deployment, and real Linux/KVM testing.
Guest networking, console access, image provisioning, and VM deletion are not
provided in part 1. IDs remain reserved until the worker exits.

The API accepts local image paths. Only trusted controllers should hold signing
credentials. Keep the default loopback binding unless HTTPS is configured.

## JWT authentication

All `/vms` endpoints and `/resources` require an RS256 bearer JWT. `/health` and
the API documentation remain public. The worker requires `exp`, `iat`, `iss`,
`aud`, and `sub`; verifies signature, expiry, optional `nbf`, issuer, audience,
and the configured controller subject. Valid controller tokens have full access
to these operations; per-operation roles are not implemented.

Generate keys on the controller machine (Linux with OpenSSL installed):

```bash
mkdir -p "$HOME/controller-keys"
chmod 700 "$HOME/controller-keys"
(umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$HOME/controller-keys/controller-private.pem")
openssl pkey -in "$HOME/controller-keys/controller-private.pem" -pubout \
  -out "$HOME/controller-keys/controller-public.pem"
```

Copy **only the public key** to each worker through your normal file transfer.
Configure and start the worker, using the public key's actual local path:

```bash
export WORKER_JWT_PUBLIC_KEY_PATH="$HOME/worker-keys/controller-public.pem"
export WORKER_JWT_ISSUER="vm-controller"
export WORKER_JWT_AUDIENCE="worker-1"
export WORKER_JWT_SUBJECT="controller"
export PYTHONPATH="$PWD/src"
python -m worker
```

Use a distinct audience for each worker. Startup fails when the key is absent,
unreadable, malformed, non-RSA, or smaller than 2048 bits. The public key is loaded
at startup; restart after changing it. No JWT authentication bypass is provided.

For a local development test, on the controller side with project dependencies:

```bash
TOKEN=$(python scripts/issue_dev_token.py \
  --private-key "$HOME/controller-keys/controller-private.pem" --audience worker-1)
curl -i http://127.0.0.1:8000/vms -H "Authorization: Bearer $TOKEN"
curl -i -X POST http://127.0.0.1:8000/vms \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @alpine-vm.json
```

This utility issues a token valid for five minutes. Regenerate it after expiration.
Apply the same Authorization header to start, stop, and resource requests; the
earlier unauthenticated examples now return 401. In `/docs`, use Authorize and
paste the token. For a remote worker, replace the URL with its HTTPS endpoint.
HTTP examples are for loopback testing only; JWT signing does not encrypt traffic.
HTTPS configuration is not added by this change. Keep clocks synchronized.

There is no token issuance/login endpoint on the worker. Controller token issuance,
automatic renewal, revocation, key rotation overlap, registration, and heartbeats
are separate work. A stolen bearer token remains usable until expiry. Do not
commit private keys or tokens. `.pem`, `.key`, and `.env` files are ignored by Git.

## Controller heartbeats

Optional: set `WORKER_HEARTBEAT_URL` to enable background reporting. Without it,
the worker continues to operate independently. Registration is not implemented.
The controller must implement the configured POST endpoint and validate the worker's
outgoing bearer JWT. This is a separate credential from incoming controller tokens;
never copy the controller's private signing key onto a worker.

```bash
export WORKER_ID="worker-1"
export WORKER_ADVERTISED_URL="https://worker-1.example:8000"
export WORKER_HEARTBEAT_URL="https://controller.example/workers/worker-1/heartbeat"
export WORKER_HEARTBEAT_TOKEN_PATH="$HOME/worker-credentials/heartbeat.jwt"
export WORKER_HEARTBEAT_INTERVAL=10
export WORKER_HEARTBEAT_TIMEOUT=5
```

Provision a controller-accepted JWT in that file, readable only by the worker user.
The file is reread each attempt; replace it atomically when an external token issuer
renews the token. Automatic token acquisition/renewal is not implemented. The worker
sends the credential; the controller is responsible for signature/claim validation
and binding the authenticated identity to the reported worker ID.

Reports begin immediately after backend initialization and are sent serially, with
the interval measured after each completed attempt. The payload is:

```json
{
  "worker_id": "worker-1",
  "worker_url": "https://worker-1.example:8000",
  "status": "ready",
  "sent_at": "2026-10-30T10:00:00+00:00",
  "resources": {
    "total_memory_mib": 4096,
    "host_reserve_mib": 512,
    "committed_memory_mib": 512,
    "allocatable_memory_mib": 3072
  }
}
```

The controller should accept any successful report with a 2xx response; no response
body is required. It should record its own receive time for liveness decisions.
Failures, including 401/404, retry indefinitely with delays of interval, 2x, 4x,
then at most 8x the interval. Successful delivery resets the delay. A 404 does not
automatically register the worker. Controller outages do not disable local VM APIs.
`status=ready` reflects successful worker initialization, not guest OS readiness.
No final offline report is guaranteed: shutdown cancels reporting before VM cleanup.

HTTPS certificate verification stays enabled, redirects are not followed, and
environment proxy settings are ignored. HTTP is allowed only for loopback URLs.
HTTPS serving on the worker/controller must be configured separately. Keep clocks
synchronized and provision the controller's CA trust on the worker if using a private CA.

## Run portable tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests use fake processes and mocked hypervisor HTTP responses. They cover lifecycle,
memory accounting, concurrent admission, disk conflicts, failure cleanup, request
validation, API errors, and readiness. They do not prove a real guest boots.