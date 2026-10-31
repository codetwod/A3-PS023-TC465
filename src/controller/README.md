# VM controller

For the complete authenticated Linux setup, follow [AUTH_SETUP.md](AUTH_SETUP.md).

Run from the repository root with Python 3.11+ and a reachable Redis server:

```bash
python -m pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
export CONTROLLER_WORKERS='{"worker-1":"https://worker-1.example:8000","worker-2":"https://worker-2.example:8000"}'
export CONTROLLER_REDIS_URL='redis://127.0.0.1:6379/0'
export CONTROLLER_PRIVATE_KEY_PATH='/run/controller/controller-private.pem'
export CONTROLLER_USERS_PATH='/run/controller/users.json'
export CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATHS='{"worker-1":"/run/controller/worker-1-public.pem","worker-2":"/run/controller/worker-2-public.pem"}'
python -m controller
```

On PowerShell, set the equivalent `$env:NAME` variables. The default bind address
is `127.0.0.1:9000`; API documentation is at `/docs`.

`POST /auth/login` accepts JSON username/password and issues a short-lived RS256
bearer token. `/auth/me` returns the current identity. VM endpoints require a user
token; users can access only their own VMs. Admins can access all VMs and `/workers`.
Pre-existing records without `owner_id` are admin-only. Heartbeat endpoints require
a separate worker JWT bound to the route's worker ID. `/health` and API docs are public.
User records and roles load at startup; restart all controller processes after edits.

By default the controller signs fresh five-minute worker tokens with its private
key. Distribute only its public key to workers. Worker audience defaults to the
configured worker ID and can be overridden by `CONTROLLER_WORKER_AUDIENCES`.
`CONTROLLER_WORKER_TOKEN_PATHS` is an optional external-token override; omit it for
automatic signing. Never give a worker the controller's private key. Use HTTPS for
remote traffic, including login: JWT signing does not encrypt passwords or requests.

## API

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | Checks Redis connectivity |
| GET | `/workers` | Live health/resources plus last heartbeat and staleness |
| POST | `/workers/{worker_id}/heartbeat` | Stores report with controller receive time; returns 204 |
| POST | `/vms` | Places and creates a VM; returns 201 |
| GET | `/vms` | Refreshes managed VM records from worker inventories |
| GET | `/vms/{vm_id}` | Refreshes and returns a specific managed VM |
| POST | `/vms/{vm_id}/start` | Starts VM on its assigned worker |
| POST | `/vms/{vm_id}/stop` | Stops VM on its assigned worker |

Create accepts the same fields as the worker (`vm_id`, `cpus`, `memory_mib`,
`kernel_path`, `disk_path`, `cmdline`). Image paths are checked by the worker,
not on the controller. Provision those paths on every candidate worker. Creation
does not boot the VM; call start separately. Worker stop remains a hard stop.

Placement probes configured nodes concurrently using `/health` and `/resources`,
filters nodes that cannot fit the memory request, and picks the greatest
`allocatable_memory_mib`, breaking ties by worker ID. CPU and disk capacity are
not reported by the existing workers. The worker still performs final admission.
A successful live probe permits placement even when heartbeats are stale; heartbeat
reports are informational and never replace live placement probes.

Workers are registered through `CONTROLLER_WORKERS`. Heartbeats must match a
configured worker ID and URL and cannot introduce or change worker addresses.
Configure each worker's existing heartbeat reporter to POST to the route above.
Both the authenticated token subject and payload identity must match the route.
Per-worker verification keys prevent one worker from impersonating another worker.

VM records contain assignment, requested configuration, latest worker details,
state, timestamps, and the last error. Redis stores JSON in `<prefix>vms` and
`<prefix>heartbeats`. A renewable Redis lock serializes mutations across controller
processes. Use the same prefix for processes managing the same cluster and a
dedicated prefix per independent cluster. Configure Redis persistence (for example
AOF), durable storage, and a no-eviction policy: losing Redis loses assignments.

The controller saves an assignment before contacting a worker. A timeout or failed
reply retains the ID and marks the outcome `unknown`; it never retries creation on
another worker. Duplicate requests return 409. Reconciliation runs at startup,
periodically, and on list/detail requests. An unreachable worker produces `unknown`;
a successful inventory without the VM produces `missing`. Neither causes automatic
recreation, migration, or release of the ID. A later inventory can recover the
actual state. Worker-only VMs are not automatically adopted. Controller persistence
does not recover the worker's in-memory configuration after a worker restart.

## Configuration

| Variable | Default |
| --- | --- |
| `CONTROLLER_HOST` | `127.0.0.1` |
| `CONTROLLER_PORT` | `9000` |
| `CONTROLLER_REDIS_URL` | `redis://127.0.0.1:6379/0` |
| `CONTROLLER_REDIS_PREFIX` | `controller:` |
| `CONTROLLER_WORKERS` | `{}` (JSON ID to base URL mapping) |
| `CONTROLLER_WORKER_TOKEN_PATHS` | `{}` (JSON ID to token file mapping) |
| `CONTROLLER_REQUEST_TIMEOUT` | `10` seconds per worker request |
| `CONTROLLER_HEARTBEAT_STALE_SECONDS` | `30` |
| `CONTROLLER_RECONCILE_INTERVAL` | `30` seconds |
| `CONTROLLER_PRIVATE_KEY_PATH` | required RSA signing key |
| `CONTROLLER_USERS_PATH` | required hashed users JSON |
| `CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATHS` | JSON worker ID to RSA public key file mapping |
| `CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH` | optional fallback public key for an external heartbeat issuer |
| `CONTROLLER_JWT_ISSUER` | `vm-controller` (must match workers) |
| `CONTROLLER_JWT_AUDIENCE` | `vm-controller-api` |
| `CONTROLLER_WORKER_AUDIENCES` | `{}`; defaults to worker IDs |
| `CONTROLLER_HEARTBEAT_ISSUER` | `worker-heartbeats` |
| `CONTROLLER_HEARTBEAT_AUDIENCE` | `vm-controller-heartbeats` |
| `CONTROLLER_TOKEN_LIFETIME` | `900` seconds; allowed 60–3600 |

HTTP transport verifies HTTPS certificates, disables environment proxies, and does
not follow redirects. HTTPS serving is configured separately. Automated tests use
mock worker HTTP responses and an in-memory Redis substitute; a real Redis/Linux
worker deployment still needs integration testing.
