#!/usr/bin/env bash

set -e

echo "[GSM] Starting GSM setup in: $(pwd)"

############################
# CHECK HOMEBREW
############################

if ! command -v brew &> /dev/null; then
    echo "[GSM] ERROR: Homebrew not installed. Install from https://brew.sh/"
    exit 1
fi

echo "[GSM] Installing C++ dependencies..."
brew install paho-mqtt-c paho-mqtt-cpp nlohmann-json apache-arrow || true

############################
# BUILD BINARIES
############################

echo "[GSM] Building ground_station_manager..."
chmod +x build_gsm_manager.sh
./build_gsm_manager.sh

if [ ! -f "./ground_station_manager" ]; then
    echo "[GSM] ERROR: ground_station_manager build failed!"
    exit 1
fi

echo "[GSM] Building ground_station_processor..."
chmod +x build_processor.sh
./build_processor.sh

if [ ! -f "./ground_station_processor" ]; then
    echo "[GSM] ERROR: ground_station_processor build failed!"
    exit 1
fi

############################
# BROKER INPUT
############################

echo ""
read -p "[GSM] Enter MQTT broker IP (default: localhost): " BROKER_IP

if [ -z "$BROKER_IP" ]; then
    BROKER="tcp://localhost:1883"
else
    if [[ "$BROKER_IP" != tcp://* ]]; then
        BROKER="tcp://$BROKER_IP:1883"
    else
        BROKER="$BROKER_IP"
    fi
fi

echo "[GSM] Using broker: $BROKER"

############################
# RUN PROCESSES
############################

cleanup() {
    echo "[GSM] Stopping GSM child processes..."
    pkill -P $$
}
trap cleanup EXIT

echo "[GSM] Starting ground station manager..."
./ground_station_manager --broker "$BROKER" &

MANAGER_PID=$!

echo "[GSM] Starting ground station processor..."
./ground_station_processor &

PROCESSOR_PID=$!

echo "[GSM] System running. Press Ctrl+C to exit."

wait $MANAGER_PID $PROCESSOR_PID