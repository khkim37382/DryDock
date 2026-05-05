#!/usr/bin/env bash
# ============================================================
# run_sms.sh
# SMS / simulation-side one-command launcher.
#
# Put this file directly inside the shared comms/ folder.
#
# Usage:
#   cd ~/comms
#   chmod +x run_sms.sh
#   ./run_sms.sh
#
# Optional:
#   ./run_sms.sh 192.168.1.50
#   ./run_sms.sh tcp://192.168.1.50:1883
# ============================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${ROOT_DIR}/logs"
VENV_DIR="${ROOT_DIR}/.venv"
REQ_FILE="${ROOT_DIR}/requirements.txt"

mkdir -p "$LOG_DIR"
mkdir -p ready_to_send_telemetry
mkdir -p received_transmissions
mkdir -p training_data

log()  { echo "[SMS] $*"; }
warn() { echo "[SMS][WARN] $*" >&2; }
die()  { echo "[SMS][ERROR] $*" >&2; exit 1; }

normalize_broker() {
    local raw="$1"

    if [[ -z "$raw" ]]; then
        die "Broker IP/address cannot be empty."
    fi

    if [[ "$raw" == tcp://* ]]; then
        echo "$raw"
    elif [[ "$raw" == *:* ]]; then
        echo "tcp://${raw}"
    else
        echo "tcp://${raw}:1883"
    fi
}

prompt_broker() {
    local supplied="${1:-}"
    local raw=""

    if [[ -n "$supplied" ]]; then
        raw="$supplied"
    else
        echo ""
        read -r -p "Enter MQTT broker IP/address, e.g. 192.168.1.50: " raw
    fi

    normalize_broker "$raw"
}

test_broker() {
    local broker="$1"
    local stripped="${broker#tcp://}"
    local host="${stripped%:*}"
    local port="${stripped##*:}"

    if command -v nc >/dev/null 2>&1; then
        log "Testing broker TCP access at ${host}:${port}..."

        if nc -z -w 3 "$host" "$port" >/dev/null 2>&1; then
            log "Broker port reachable."
        else
            warn "Could not reach broker port. Continuing anyway; MQTT may fail if the broker is not running."
        fi
    else
        warn "nc not found; skipping broker reachability check."
    fi
}

cleanup() {
    log "Stopping SMS child processes..."

    if [[ -n "${DISPATCHER_PID:-}" ]]; then
        kill "$DISPATCHER_PID" >/dev/null 2>&1 || true
    fi
}

trap cleanup INT TERM EXIT

log "Starting SMS setup in: $ROOT_DIR"

if [[ ! -f "satellite_orbit.py" ]]; then
    die "satellite_orbit.py not found in $ROOT_DIR"
fi

if [[ ! -f "build_dispatcher.sh" ]]; then
    die "build_dispatcher.sh not found in $ROOT_DIR"
fi

if [[ ! -f "dispatcher.cpp" ]]; then
    die "dispatcher.cpp not found in $ROOT_DIR"
fi

log "Checking Python..."

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found. Install Python 3 first."
fi

if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating Python virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    log "Python virtual environment already exists."
fi

source "${VENV_DIR}/bin/activate"

log "Upgrading pip..."
python -m pip install --upgrade pip setuptools wheel

if [[ ! -f "$REQ_FILE" ]]; then
    warn "requirements.txt not found. Creating baseline requirements.txt."

    cat > "$REQ_FILE" <<'EOF'
vpython
numpy
pandas
pyarrow
paho-mqtt
watchdog
requests
EOF
fi

log "Installing/checking Python packages from requirements.txt..."
python -m pip install -r "$REQ_FILE"

log "Verifying simulation Python imports..."

python - <<'PY'
import importlib

required = ["vpython", "numpy"]
missing = []

for pkg in required:
    try:
        importlib.import_module(pkg)
    except Exception as e:
        missing.append((pkg, str(e)))

if missing:
    for pkg, err in missing:
        print(f"Missing/broken package: {pkg}: {err}")
    raise SystemExit(1)

print("Python simulation imports OK.")
PY

log "Building/checking dispatcher executable..."
chmod +x build_dispatcher.sh
./build_dispatcher.sh

if [[ ! -x "./dispatcher" ]]; then
    die "dispatcher executable was not produced or is not executable."
fi

BROKER="$(prompt_broker "${1:-}")"
export MQTT_BROKER="$BROKER"

log "Using MQTT broker: $BROKER"
test_broker "$BROKER"

log "Starting dispatcher in background..."

./dispatcher --broker "$BROKER" > "${LOG_DIR}/sms_dispatcher.log" 2>&1 &
DISPATCHER_PID=$!

sleep 2

if ! kill -0 "$DISPATCHER_PID" >/dev/null 2>&1; then
    warn "Dispatcher exited early. Last dispatcher log lines:"
    tail -n 80 "${LOG_DIR}/sms_dispatcher.log" || true
    die "Dispatcher failed to start."
fi

log "Dispatcher PID: $DISPATCHER_PID"
log "Dispatcher log: ${LOG_DIR}/sms_dispatcher.log"

echo ""
log "Starting satellite_orbit.py in the foreground."
log "Answer the simulation setup prompts in this terminal."
echo ""

python satellite_orbit.py