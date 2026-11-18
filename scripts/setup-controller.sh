#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state="$HOME/.local/share/vm-controller"
python="$root/.venv/bin/python"
action="${1:-setup}"

fail() { printf '%s\n' "$*" >&2; exit 1; }

case "$action" in
    -h|--help) echo "Usage: bash scripts/setup-controller.sh [setup|start|check]"; exit ;;
    setup|start|check) ;;
    *) fail "Use setup, start, or check." ;;
esac
[[ $# -le 1 && "$(uname -s)" == Linux ]] || fail "Run this script on Linux with at most one action."

export PYTHONPATH="$root/src"
export CONTROLLER_HOST=127.0.0.1 CONTROLLER_PORT=9000
export CONTROLLER_REDIS_URL="${CONTROLLER_REDIS_URL:-redis://127.0.0.1:6379/0}"
export CONTROLLER_REDIS_PREFIX="${CONTROLLER_REDIS_PREFIX:-controller:setup:}"
export CONTROLLER_PRIVATE_KEY_PATH="$state/controller-private.pem"
export CONTROLLER_USERS_PATH="$state/users.json"
export CONTROLLER_WORKERS='{"worker-1":"http://127.0.0.1:18000"}'
export CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH="$state/heartbeat-public.pem"
unset CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATHS CONTROLLER_WORKER_TOKEN_PATHS CONTROLLER_WORKER_AUDIENCES

redis_ready() {
    "$python" - <<'PY'
import os
from redis import Redis
from redis.exceptions import RedisError

try:
    with Redis.from_url(os.environ["CONTROLLER_REDIS_URL"], socket_timeout=2,
                        socket_connect_timeout=2) as redis:
        redis.ping()
except (RedisError, ValueError):
    raise SystemExit(1)
PY
}

if [[ "$action" == setup ]]; then
    administrator=()
    (( EUID == 0 )) || administrator=(sudo)
    packages=()
    command -v openssl >/dev/null || packages+=(openssl)
    python3 -c 'import ensurepip, sys; assert sys.version_info >= (3, 11)' 2>/dev/null ||
        packages+=(python3 python3-venv)
    if [[ "$CONTROLLER_REDIS_URL" == redis://127.0.0.1:6379/0 ]] &&
        ! command -v redis-server >/dev/null; then
        packages+=(redis-server)
    fi
    if [[ "${SETUP_CONTROLLER_SSH:-0}" == 1 && ! -x /usr/sbin/sshd ]]; then
        packages+=(openssh-server)
    fi
    if (( ${#packages[@]} )); then
        command -v apt-get >/dev/null || fail "Install these packages with your distribution's package manager: ${packages[*]}"
        "${administrator[@]}" apt-get update
        "${administrator[@]}" apt-get install -y "${packages[@]}"
    fi
    if [[ "${SETUP_CONTROLLER_SSH:-0}" == 1 ]]; then
        "${administrator[@]}" systemctl start ssh ||
            fail "Start your SSH service and verify that it allows loopback TCP forwarding."
    fi
    python3 -c 'import sys; assert sys.version_info >= (3, 11)' || fail "Python 3.11 or newer is required."
    [[ -x "$python" ]] || python3 -m venv "$root/.venv"
    "$python" -m pip install -r "$root/requirements.txt"
    "$python" -m pip check

    if ! redis_ready; then
        [[ "$CONTROLLER_REDIS_URL" == redis://127.0.0.1:6379/0 ]] ||
            fail "Cannot reach the configured Redis. Check its address and credentials."
        "${administrator[@]}" systemctl start redis-server ||
            fail "Start your local Redis service or set CONTROLLER_REDIS_URL to a reachable Redis instance."
        redis_ready || fail "Redis started but did not answer PING."
    fi

    install -d -m 700 "$state"
    if [[ ! -e "$CONTROLLER_PRIVATE_KEY_PATH" ]]; then
        temporary="$(mktemp "$state/.private.XXXXXX")"
        trap 'rm -f -- "$temporary"' EXIT
        openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$temporary"
        ln -- "$temporary" "$CONTROLLER_PRIVATE_KEY_PATH"
    fi
    openssl pkey -in "$CONTROLLER_PRIVATE_KEY_PATH" -pubout -out "$state/controller-public.pem"
    if [[ ! -s "$CONTROLLER_USERS_PATH" ]]; then
        "$python" "$root/scripts/create_controller_user.py" --users-file "$CONTROLLER_USERS_PATH" --username admin --role admin
    fi
    printf 'Controller setup complete. Give the worker operator %s.\n' "$state/controller-public.pem"
    printf 'Install the worker heartbeat public key at %s, then run this script with start.\n' "$CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH"
    exit 0
fi

[[ -x "$python" && -s "$CONTROLLER_PRIVATE_KEY_PATH" && -s "$CONTROLLER_USERS_PATH" ]] ||
    fail "Run controller setup first."
[[ -s "$CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH" ]] ||
    fail "Install the worker heartbeat public key at $CONTROLLER_HEARTBEAT_PUBLIC_KEY_PATH."

if [[ "$action" == start ]]; then
    redis_ready || fail "Redis is unreachable. Run controller setup or start the configured Redis instance."
    exec "$python" -m controller
fi

exec "$python" - <<'PY'
import time
from datetime import datetime, timezone

import httpx

from controller.auth import Auth
from controller.config import Settings

settings = Settings.from_env()
auth = Auth(settings)
admin = next((name for name, user in auth.users.items()
              if user.get("role") == "admin" and not user.get("disabled")), None)
if admin is None:
    raise SystemExit("Connection check requires an enabled controller admin.")
token = auth.token(admin, settings.jwt_audience, 180, token_type="user")

started = datetime.now(timezone.utc)
deadline = time.monotonic() + 100
with httpx.Client(base_url="http://127.0.0.1:9000", trust_env=False, timeout=25,
                  headers={"Authorization": f"Bearer {token}"}) as client:
    while True:
        try:
            response = client.get("/workers")
            response.raise_for_status()
            worker = next((entry for entry in response.json()
                           if entry["worker_id"] == "worker-1"), None)
            report = worker["heartbeat"] if worker else None
            received = report and datetime.fromisoformat(report["received_at"])
            fresh = worker and not worker["heartbeat_stale"] and received and received >= started
            if worker and worker["healthy"] and fresh:
                print("Connected: controller authenticated to worker-1 and received a fresh signed heartbeat.")
                break
            probe = "OK" if worker and worker["healthy"] else "failed"
            heartbeat = "received" if fresh else "no new heartbeat"
            status = f"authenticated worker probe: {probe}, signed heartbeat: {heartbeat}"
        except httpx.HTTPStatusError as exc:
            status = f"controller /workers returned HTTP {exc.response.status_code}"
        except httpx.RequestError as exc:
            status = f"controller /workers unavailable ({type(exc).__name__})"
        if time.monotonic() >= deadline:
            raise SystemExit(f"Connection check failed: {status}. Check both service logs.")
        time.sleep(2)
PY
