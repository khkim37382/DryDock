#!/usr/bin/env bash
# ============================================================
# build_gsm_manager.sh — GSM Mac build for ground_station_manager.cpp
# ============================================================
# Auto-detects Homebrew prefix (Apple Silicon / Intel).
#
# USAGE:
#   ./build_gsm_manager.sh                               # build only
#   ./build_gsm_manager.sh --run                         # build + run (localhost)
#   ./build_gsm_manager.sh --run --broker 192.168.1.50   # build + run (remote)
#   ./build_gsm_manager.sh --run --broker tcp://192.168.1.50:1883
#
# After building, run directly:
#   ./ground_station_manager
#   ./ground_station_manager --broker tcp://192.168.1.50:1883
#
# IMPORTANT: dispatcher and ground_station_manager must point to the
# SAME broker IP, otherwise no messages will flow.
#
# Creates session folder automatically:
#   received_transmissions/YYYY-MM-DDTHH-MM-SS/json/
#   received_transmissions/YYYY-MM-DDTHH-MM-SS/parquet/
#
# Dependencies (run once):
#   brew install eclipse-paho-mqtt-cpp nlohmann-json
# ============================================================
set -euo pipefail

BROKER_ARG=""
RUN_AFTER_BUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run)    RUN_AFTER_BUILD=true; shift ;;
        --broker) BROKER_ARG="$2";      shift 2 ;;
        *) echo "Unknown arg: $1"; echo "Usage: $0 [--run] [--broker HOST:PORT]"; exit 1 ;;
    esac
done

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew not found. Install from https://brew.sh"; exit 1
fi
BREW_PREFIX="$(brew --prefix)"
echo "Homebrew prefix: ${BREW_PREFIX}"

need_install=()
for pkg in eclipse-paho-mqtt-cpp nlohmann-json; do
    brew list --formula --versions "${pkg}" >/dev/null 2>&1 || need_install+=("${pkg}")
done
[[ ${#need_install[@]} -gt 0 ]] && { echo "Installing: ${need_install[*]}"; brew install "${need_install[@]}"; }

INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

[[ -f "${INC_DIR}/mqtt/async_client.h" ]] || { echo "ERROR: paho missing.    Run: brew install eclipse-paho-mqtt-cpp"; exit 1; }
[[ -f "${INC_DIR}/nlohmann/json.hpp"   ]] || { echo "ERROR: nlohmann missing. Run: brew install nlohmann-json";         exit 1; }

echo "Compiling ground_station_manager.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    ground_station_manager.cpp \
    -lpaho-mqttpp3 -lpaho-mqtt3a \
    -o ground_station_manager

echo "Build OK: ./ground_station_manager"
echo ""
echo "Run examples:"
echo "  ./ground_station_manager"
echo "  ./ground_station_manager --broker tcp://192.168.1.50:1883"
echo ""
echo "NOTE: Both dispatcher (SMS) and ground_station_manager (GSM) must"
echo "      point to the SAME broker IP."

if [[ "${RUN_AFTER_BUILD}" == true ]]; then
    echo ""
    if [[ -n "${BROKER_ARG}" ]]; then
        echo "Starting: ./ground_station_manager --broker ${BROKER_ARG}"
        exec ./ground_station_manager --broker "${BROKER_ARG}"
    else
        echo "Starting: ./ground_station_manager"
        exec ./ground_station_manager
    fi
fi
