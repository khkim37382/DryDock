#!/usr/bin/env bash
set -e

VENV_DIR=".venv"
DISPATCHER_BIN="./dispatcher"
LOG_FILE="session.log"

> "$LOG_FILE"

echo "--- SMS Execution Suite ---"

# 1. Environment Setup
if [ ! -d "$VENV_DIR" ]; then
    echo "[SETUP] Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip setuptools==69.5.1 vpython paho-mqtt numpy --quiet
else
    source "$VENV_DIR/bin/activate"
fi

export PYTHONPATH="$PYTHONPATH:$(python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || echo '')"

# 2. Setup Connectivity
if [ -f "ip.txt" ]; then
    BROKER=$(head -n 1 ip.txt)
    echo "[CONN] Using saved IP: $BROKER"
else
    read -p "Enter Broker (Mac/Dell) IP: " BROKER
fi

# 3. BACKGROUND THE DISPATCHER FIRST
echo "[RUN] Starting Dispatcher (Logging to $LOG_FILE)..."
# We background this so it doesn't block the script
$DISPATCHER_BIN --broker "$BROKER" >> "$LOG_FILE" 2>&1 &
DISPATCHER_PID=$!

# 4. FOREGROUND THE SIMULATION (The final boss)
echo "[RUN] Starting Simulation Setup..."
echo "------------------------------------------------"
# NO AMPERSAND HERE. This gives Python full CLI yield.
python3 satellite_orbit.py 

# 5. Cleanup (Runs after you exit Python/Ctrl+C)
cleanup() { 
    echo -e "\n[EXIT] Cleaning up background processes..."
    kill "$DISPATCHER_PID" 2>/dev/null || true
    exit 
}
trap cleanup EXIT INT TERM