#!/usr/bin/env bash
set -e

GSM_BIN="./ground_station_manager"
PROC_BIN="./ground_station_processor"

echo "--- GSM Ground Station Suite ---"

# STEP 1: System Dependencies & Binaries
# We check if BOTH binaries exist. If one is missing, we run the setup.
if [[ ! -f "$GSM_BIN" || ! -f "$PROC_BIN" ]]; then
    echo "[SETUP] First run or missing binaries detected. Installing dependencies..."
    # Homebrew is smart enough to skip if already installed, but we only call it here.
    brew install libpaho-mqtt nlohmann-json apache-arrow || true
    
    echo "[SETUP] Building GSM Manager and Processor..."
    chmod +x build_gsm_manager.sh build_processor.sh
    ./build_gsm_manager.sh
    ./build_processor.sh
else
    echo "[READY] GSM binaries found. Skipping build."
fi

# STEP 2: Broker IP
if [ -f "ip.txt" ]; then
    BROKER=$(head -n 1 ip.txt)
    echo "[CONN] Using Broker IP from ip.txt: $BROKER"
else
    read -p "Enter Dell IP: " BROKER
fi

# STEP 3: Launch
cleanup() { kill "$MANAGER_PID" "$PROCESSOR_PID" 2>/dev/null || true; exit; }
trap cleanup EXIT INT TERM

$GSM_BIN --broker "$BROKER" &
MANAGER_PID=$!
sleep 2
$PROC_BIN &
PROCESSOR_PID=$!
wait