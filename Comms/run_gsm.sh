#!/usr/bin/env bash
# ============================================================
# run_gsm.sh
# GSM / ground-station-side one-command launcher.
#
# Put this file directly inside the shared comms/ folder.
#
# Usage:
#   cd ~/comms
#   chmod +x run_gsm.sh
#   ./run_gsm.sh
#
# Optional:
#   ./run_gsm.sh 192.168.1.50
#   ./run_gsm.sh tcp://192.168.1.50:1883
# ============================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${ROOT_DIR}/logs"

mkdir -p "$LOG_DIR"
mkdir -p received_transmissions
mkdir -p training_data

log()  { echo "[GSM] $*"; }
warn() { echo "[GSM][WARN] $*" >&2; }
die()  { echo "[GSM][ERROR] $*" >&2; exit 1; }

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
    log "Stopping GSM child processes..."

    if [[ -n "${GSM_PID:-}" ]]; then
        kill "$GSM_PID" >/dev/null 2>&1 || true
    fi

    if [[ -n "${PROC_PID:-}" ]]; then
        kill "$PROC_PID" >/dev/null 2>&1 || true
    fi
}

trap cleanup INT TERM EXIT

log "Starting GSM setup in: $ROOT_DIR"

for f in build_gsm_manager.sh build_processor.sh ground_station_manager.cpp ground_station_processor.cpp; do
    if [[ ! -f "$f" ]]; then
        die "$f not found in $ROOT_DIR"
    fi
done

log "Building/checking ground_station_manager executable..."
chmod +x build_gsm_manager.sh
./build_gsm_manager.sh

if [[ ! -x "./ground_station_manager" ]]; then
    die "ground_station_manager executable was not produced or is not executable."
fi

log "Building/checking ground_station_processor executable..."
chmod +x build_processor.sh
./build_processor.sh

if [[ ! -x "./ground_station_processor" ]]; then
    die "ground_station_processor executable was not produced or is not executable."
fi

BROKER="$(prompt_broker "${1:-}")"
export MQTT_BROKER="$BROKER"

log "Using MQTT broker: $BROKER"
test_broker "$BROKER"

log "Starting ground_station_manager..."

./ground_station_manager --broker "$BROKER" > "${LOG_DIR}/gsm_manager.log" 2>&1 &
GSM_PID=$!

sleep 3

if ! kill -0 "$GSM_PID" >/dev/null 2>&1; then
    warn "Ground station manager exited early. Last manager log lines:"
    tail -n 80 "${LOG_DIR}/gsm_manager.log" || true
    die "ground_station_manager failed to start."
fi

log "Starting ground_station_processor..."

./ground_station_processor > "${LOG_DIR}/gsm_processor.log" 2>&1 &
PROC_PID=$!

sleep 2

if ! kill -0 "$PROC_PID" >/dev/null 2>&1; then
    warn "Processor exited early. Last processor log lines:"
    tail -n 80 "${LOG_DIR}/gsm_processor.log" || true
    die "ground_station_processor failed to start."
fi

log "GSM manager PID: $GSM_PID"
log "Processor PID: $PROC_PID"
log "Manager log: ${LOG_DIR}/gsm_manager.log"
log "Processor log: ${LOG_DIR}/gsm_processor.log"

echo ""
log "GSM is running. Press Ctrl+C to stop both GSM processes."
echo ""

wait