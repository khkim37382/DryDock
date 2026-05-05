#!/usr/bin/env bash

set -e

echo "[SMS] Starting SMS setup in: $(pwd)"

############################
# PYTHON ENV SETUP
############################

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[SMS] Creating virtual environment..."
    python3 -m venv $VENV_DIR
fi

echo "[SMS] Activating virtual environment..."
source $VENV_DIR/bin/activate

echo "[SMS] Upgrading core Python tooling..."
pip install --upgrade pip setuptools wheel

if [ ! -f "reqs.txt" ]; then
    echo "[SMS] ERROR: reqs.txt not found!"
    exit 1
fi

echo "[SMS] Installing Python dependencies..."
pip install -r reqs.txt

echo "[SMS] Verifying Python imports..."
python3 - <<EOF
import pkg_resources
import json
import threading
print("Core Python packages OK")

try:
    import vpython
    print("vpython OK")
except Exception as e:
    print("WARNING: vpython issue:", e)
EOF

############################
# BUILD DISPATCHER
############################

if ! command -v brew &> /dev/null; then
    echo "[SMS] ERROR: Homebrew not installed. Install from https://brew.sh/"
    exit 1
fi

echo "[SMS] Installing C++ dependencies..."
brew install paho-mqtt-c paho-mqtt-cpp nlohmann-json || true

echo "[SMS] Building dispatcher..."
chmod +x build_dispatcher.sh
./build_dispatcher.sh

if [ ! -f "./dispatcher" ]; then
    echo "[SMS] ERROR: dispatcher build failed!"
    exit 1
fi

############################
# BROKER INPUT
############################

echo ""
read -p "[SMS] Enter MQTT broker IP (default: localhost): " BROKER_IP

if [ -z "$BROKER_IP" ]; then
    BROKER="tcp://localhost:1883"
else
    if [[ "$BROKER_IP" != tcp://* ]]; then
        BROKER="tcp://$BROKER_IP:1883"
    else
        BROKER="$BROKER_IP"
    fi
fi

echo "[SMS] Using broker: $BROKER"

############################
# RUN PROCESSES
############################

cleanup() {
    echo "[SMS] Stopping SMS child processes..."
    pkill -P $$
}
trap cleanup EXIT

echo "[SMS] Starting dispatcher..."
./dispatcher --broker "$BROKER" &

DISPATCHER_PID=$!

echo "[SMS] Starting simulation..."
python3 simulation.py &

SIM_PID=$!

echo "[SMS] System running. Press Ctrl+C to exit."

wait $DISPATCHER_PID $SIM_PID