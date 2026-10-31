# VM controller

Run from the repository root with Python 3.11+ and a reachable Redis server:

```bash
python -m pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
export CONTROLLER_WORKERS='{"worker-1":"https://worker-1.example:8000","worker-2":"https://worker-2.example:8000"}'
export CONTROLLER_WORKER_TOKEN_PATHS='{"worker-1":"/run/controller/worker-1.jwt","worker-2":"/run/controller/worker-2.jwt"}'
export CONTROLLER_REDIS_URL='redis://127.0.0.1:6379/0'
python -m controller
```

On PowerShell, set the equivalent `$env:NAME` variables. The default bind address
is `127.0.0.1:9000`; API documentation is at `/docs`.

Authentication and user ownership checks are intentionally deferred. All controller
routes, including heartbeat ingestion, currently accept unauthenticated callers.
Keep the controller on a trusted private network until authentication is added.
Existing worker authentication is unchanged: provision worker-specific controller
JWTs using `scripts/issue_dev_token.py` or an external issuer. Token files are read
on every outgoing request so externally renewed tokens take effect immediately.
This controller does not issue, renew, or verify JWTs. Token paths can be omitted
for test doubles; existing real workers require valid tokens.

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
Identity matching currently checks payload values only, with no authentication.

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

HTTP transport verifies HTTPS certificates, disables environment proxies, and does
not follow redirects. HTTPS serving is configured separately. Automated tests use
mock worker HTTP responses and an in-memory Redis substitute; a real Redis/Linux
worker deployment still needs integration testing.
