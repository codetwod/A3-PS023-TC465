# Authenticated Linux setup

All commands start at the repository root. Python 3.11+, Redis, and the worker's
Linux/KVM/Cloud Hypervisor prerequisites are required. Install on Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
```

## 1. Provision keys and users

On the controller host:

```bash
mkdir -p "$HOME/controller-keys"
chmod 700 "$HOME/controller-keys"
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$HOME/controller-keys/controller-private.pem"
openssl pkey -in "$HOME/controller-keys/controller-private.pem" -pubout \
  -out "$HOME/controller-keys/controller-public.pem"
python scripts/create_controller_user.py \
  --users-file "$HOME/controller-keys/users.json" --username admin --role admin
python scripts/create_controller_user.py \
  --users-file "$HOME/controller-keys/users.json" --username alice --role user
```

The provisioning script prompts for passwords and stores salted scrypt hashes.
There is no public signup endpoint. Run provisioning while the controller is stopped,
or restart all controller processes afterward. To disable a user, set `"disabled": true`
in their record and restart; tokens are checked against the loaded user registry on
every request. Roles also come from that registry, not from caller-supplied claims.
Changing a password does not invalidate existing tokens; disable the account until
the previous token lifetime has elapsed when immediate revocation is needed.

On **each worker**, generate its own heartbeat signing key:

```bash
mkdir -p "$HOME/worker-keys"
chmod 700 "$HOME/worker-keys"
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$HOME/worker-keys/heartbeat-private.pem"
openssl pkey -in "$HOME/worker-keys/heartbeat-private.pem" -pubout \
  -out "$HOME/worker-keys/heartbeat-public.pem"
```

Copy the controller **public** key to each worker as
`$HOME/worker-keys/controller-public.pem`. Copy each worker's heartbeat **public**
key to the controller as `$HOME/controller-keys/worker-1-public.pem` and
`worker-2-public.pem`. Private keys stay on the machine that generated them.

## 2. Start controller

Start Redis with persistent storage/AOF and no eviction. Set the reachable HTTPS
worker endpoints; HTTPS serving needs your TLS reverse proxy or service configuration.

```bash
export PYTHONPATH="$PWD/src"
export CONTROLLER_REDIS_URL='redis://127.0.0.1:6379/0'
export CONTROLLER_PRIVATE_KEY_PATH="$HOME/controller-keys/controller-private.pem"
export CONTROLLER_USERS_PATH="$HOME/controller-keys/users.json"
export CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATHS="{\"worker-1\":\"$HOME/controller-keys/worker-1-public.pem\",\"worker-2\":\"$HOME/controller-keys/worker-2-public.pem\"}"
export CONTROLLER_WORKERS='{"worker-1":"https://worker-1.example:8000","worker-2":"https://worker-2.example:8000"}'
unset CONTROLLER_WORKER_TOKEN_PATHS
python -m controller
```

The controller listens on loopback port 9000 for a local reverse proxy. Publish it
as `https://controller.example` for remote worker heartbeats and user access.
Provision trusted CA certificates on both sides if using private certificates.

## 3. Start workers

On worker 1 (on worker 2, replace both occurrences of `worker-1` and its advertised URL):

```bash
export PYTHONPATH="$PWD/src"
export WORKER_ID='worker-1'
export WORKER_JWT_PUBLIC_KEY_PATH="$HOME/worker-keys/controller-public.pem"
export WORKER_JWT_ISSUER='vm-controller'
export WORKER_JWT_AUDIENCE='worker-1'
export WORKER_JWT_SUBJECT='controller'
export WORKER_ADVERTISED_URL='https://worker-1.example:8000'
export WORKER_HEARTBEAT_URL='https://controller.example/workers/worker-1/heartbeat'
export WORKER_HEARTBEAT_PRIVATE_KEY_PATH="$HOME/worker-keys/heartbeat-private.pem"
unset WORKER_HEARTBEAT_TOKEN_PATH
python -m worker
```

Run one process per worker runtime directory. Each worker binds loopback port 8000
by default; put its TLS proxy in front. Image paths supplied to create must exist
on every eligible worker, with distinct writable disks for concurrent VMs.
Workers sign a fresh heartbeat token per report. Both directions renew credentials
automatically without copying the controller private key to any worker.

For a single-machine Linux smoke test, replace all advertised/base URLs with
`http://127.0.0.1:<port>`, use controller port 9000 and distinct worker ports
8001/8002, and set distinct `WORKER_RUNTIME_DIR` values. Match controller registry
URLs, worker advertised URLs, and heartbeat URLs exactly. Remote HTTP is rejected
by the worker heartbeat configuration; use HTTPS across machines.

## 4. Login and use VMs

```bash
curl -fsS https://controller.example/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"YOUR_PASSWORD"}'
```

Set `TOKEN` to the returned `access_token`, then:

```bash
curl -fsS https://controller.example/auth/me -H "Authorization: Bearer $TOKEN"
curl -fsS -X POST https://controller.example/vms \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"vm_id":"demo","cpus":1,"memory_mib":512,"kernel_path":"/srv/vms/vmlinux","disk_path":"/srv/vms/demo.raw"}'
curl -fsS -X POST https://controller.example/vms/demo/start -H "Authorization: Bearer $TOKEN"
curl -fsS https://controller.example/vms/demo -H "Authorization: Bearer $TOKEN"
curl -fsS -X POST https://controller.example/vms/demo/stop -H "Authorization: Bearer $TOKEN"
```

Login again after token expiry (15 minutes by default). Login has Redis-backed
limits of 10 attempts per username and 30 per source IP per minute; it fails closed
when Redis is unavailable. Configure trusted proxy/source-IP forwarding at your
deployment boundary so clients cannot spoof the limiter identity. Do not log login
request bodies, passwords, or bearer tokens. Keep keys and users JSON mode 0600.

`/workers` requires an admin token. Unowned legacy VM records are admin-only;
assign `owner_id` through an operator-controlled Redis update if ownership must
be migrated. A user cannot set ownership through the create request.

## Verification

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests include real controller/worker HTTP APIs and RSA JWT verification with a
mock hypervisor backend. Real Linux/KVM boot and persistent Redis behavior need
deployment checks. Check both workers appear healthy in `/workers`, heartbeats
are fresh, and a create/start/detail/stop cycle succeeds with your actual images.
