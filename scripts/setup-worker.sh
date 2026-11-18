#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state="$HOME/.local/share/vm-worker"
python="$root/.venv/bin/python"
action="${1:-setup}"
ssh_target="${2:-}"

fail() { printf '%s\n' "$*" >&2; exit 1; }

case "$action" in
    -h|--help) echo "Usage: bash scripts/setup-worker.sh [setup|start|check] [user@controller-host]"; exit ;;
    setup|start|check) ;;
    *) fail "Use setup, start, or check." ;;
esac
[[ $# -le 2 && "$(uname -s)" == Linux ]] || fail "Run this script on Linux with at most two arguments."

export PYTHONPATH="$root/src"
export WORKER_ID=worker-1 WORKER_HOST=127.0.0.1 WORKER_PORT=18000
export WORKER_ADVERTISED_URL=http://127.0.0.1:18000
controller_port=9000
if [[ -n "$ssh_target" ]]; then
    WORKER_PORT=8000
    controller_port=19000
fi
export WORKER_HEARTBEAT_URL="http://127.0.0.1:$controller_port/workers/$WORKER_ID/heartbeat"
export WORKER_JWT_PUBLIC_KEY_PATH="$state/controller-public.pem"
export WORKER_HEARTBEAT_PRIVATE_KEY_PATH="$state/heartbeat-private.pem"
export WORKER_JWT_AUDIENCE="$WORKER_ID" WORKER_JWT_SUBJECT=controller
export WORKER_RUNTIME_DIR="${WORKER_RUNTIME_DIR:-/tmp/ch-worker-$UID}"
unset WORKER_HEARTBEAT_TOKEN_PATH

hypervisor="${WORKER_HYPERVISOR_BINARY:-cloud-hypervisor}"
if [[ -z "${WORKER_HYPERVISOR_BINARY:-}" ]] && ! command -v "$hypervisor" >/dev/null; then
    hypervisor="$state/cloud-hypervisor"
fi
export WORKER_HYPERVISOR_BINARY="$hypervisor"

check_kvm() {
    "$python" - <<'PY'
import fcntl

try:
    with open("/dev/kvm", "r+b", buffering=0) as kvm:
        if fcntl.ioctl(kvm, 0xAE00) != 12:  # KVM_GET_API_VERSION
            raise SystemExit("Unsupported KVM API version.")
except OSError as exc:
    raise SystemExit(f"KVM unavailable: {exc}. Enable host virtualization (or nested KVM) "
                     "and grant this Linux user access to /dev/kvm.")
PY
}

if [[ "$action" == setup ]]; then
    packages=()
    command -v openssl >/dev/null || packages+=(openssl)
    command -v curl >/dev/null || packages+=(curl ca-certificates)
    if [[ -n "$ssh_target" ]] && ! command -v ssh >/dev/null; then
        packages+=(openssh-client)
    fi
    python3 -c 'import ensurepip, sys; assert sys.version_info >= (3, 11)' 2>/dev/null ||
        packages+=(python3 python3-venv)
    if (( ${#packages[@]} )); then
        command -v apt-get >/dev/null || fail "Install these packages with your distribution's package manager: ${packages[*]}"
        administrator=()
        (( EUID == 0 )) || administrator=(sudo)
        "${administrator[@]}" apt-get update
        "${administrator[@]}" apt-get install -y "${packages[@]}"
    fi
    python3 -c 'import sys; assert sys.version_info >= (3, 11)' || fail "Python 3.11 or newer is required."
    [[ -x "$python" ]] || python3 -m venv "$root/.venv"
    "$python" -m pip install -r "$root/requirements.txt"
    "$python" -m pip check

    install -d -m 700 "$state" "$WORKER_RUNTIME_DIR"
    temporary="$(mktemp -d "$state/.setup.XXXXXX")"
    trap 'rm -f -- "$temporary/hypervisor" "$temporary/private.pem"; rmdir -- "$temporary"' EXIT
    if [[ "$hypervisor" == "$state/cloud-hypervisor" && ! -x "$hypervisor" ]]; then
        case "$(uname -m)" in
            x86_64) asset=cloud-hypervisor-static ;;
            aarch64) asset=cloud-hypervisor-static-aarch64 ;;
            *) fail "Set WORKER_HYPERVISOR_BINARY to a compatible Cloud Hypervisor executable." ;;
        esac
        release="${CLOUD_HYPERVISOR_RELEASE:-v54.1}"
        [[ "$release" =~ ^v[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail "Invalid Cloud Hypervisor release tag."
        curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --retry 2 "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$release/$asset" \
            --output "$temporary/hypervisor"
        chmod 700 "$temporary/hypervisor"
        "$temporary/hypervisor" --version
        mv -- "$temporary/hypervisor" "$hypervisor"
    fi
    "$hypervisor" --version || fail "Set WORKER_HYPERVISOR_BINARY to a working Cloud Hypervisor executable."
    if [[ ! -e "$WORKER_HEARTBEAT_PRIVATE_KEY_PATH" ]]; then
        openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$temporary/private.pem"
        ln -- "$temporary/private.pem" "$WORKER_HEARTBEAT_PRIVATE_KEY_PATH"
    fi
    openssl pkey -in "$WORKER_HEARTBEAT_PRIVATE_KEY_PATH" -pubout -out "$state/heartbeat-public.pem"
    printf 'Give the controller operator %s.\n' "$state/heartbeat-public.pem"
    printf 'Install the controller public key at %s.\n' "$WORKER_JWT_PUBLIC_KEY_PATH"
    check_kvm
    echo "Worker setup complete. Run this script with start once the controller is configured."
    exit 0
fi

[[ -x "$python" && -s "$WORKER_HEARTBEAT_PRIVATE_KEY_PATH" ]] || fail "Run worker setup first."
if [[ "$action" == check ]]; then
    curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        "http://127.0.0.1:$WORKER_PORT/health" >/dev/null || fail "Worker API is unavailable."
    curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        "http://127.0.0.1:$controller_port/health" >/dev/null || fail "Controller API is unavailable from the worker."
    echo "Both APIs are reachable from the worker. Run setup-controller.sh check to verify authentication and heartbeats."
    exit 0
fi

[[ -s "$WORKER_JWT_PUBLIC_KEY_PATH" ]] || fail "Install the controller public key at $WORKER_JWT_PUBLIC_KEY_PATH."
check_kvm
"$hypervisor" --version || fail "Run worker setup or fix WORKER_HYPERVISOR_BINARY."
if [[ -z "$ssh_target" ]]; then
    exec "$python" -m worker
fi

tunnel_pid=
worker_pid=
cleanup() {
    [[ -z "$tunnel_pid" ]] || kill "$tunnel_pid" 2>/dev/null || true
    [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
    wait
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ssh -nNT -S none -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes \
    -o ForwardAgent=no -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:$controller_port:127.0.0.1:9000" \
    -R "127.0.0.1:18000:127.0.0.1:$WORKER_PORT" -- "$ssh_target" &
tunnel_pid=$!
"$python" -m worker &
worker_pid=$!
status=0
wait -n "$tunnel_pid" "$worker_pid" || status=$?
printf 'Worker service or SSH tunnel exited (status %s). Check the output above.\n' "$status" >&2
(( status != 0 )) || status=1
exit "$status"
