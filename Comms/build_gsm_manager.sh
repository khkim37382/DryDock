#!/usr/bin/env bash
# ============================================================
# build_gsm_manager.sh — macOS build for ground_station_manager.cpp
# ============================================================
# Auto-detects Homebrew prefix (Apple Silicon vs Intel).
#
# Dependencies (install once):
#   brew install eclipse-paho-mqtt-cpp nlohmann-json
#
# Usage:
#   chmod +x build_gsm_manager.sh
#   ./build_gsm_manager.sh
# ============================================================
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew not found. Install from https://brew.sh"
    exit 1
fi
BREW_PREFIX="$(brew --prefix)"
echo "Homebrew prefix: ${BREW_PREFIX}"

# ── Verify dependencies ──────────────────────────────────────
need_install=()
for pkg in eclipse-paho-mqtt-cpp nlohmann-json; do
    if ! brew list --formula --versions "${pkg}" >/dev/null 2>&1; then
        need_install+=("${pkg}")
    fi
done
if [[ ${#need_install[@]} -gt 0 ]]; then
    echo "Installing missing packages: ${need_install[*]}"
    brew install "${need_install[@]}"
fi

INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

# Sanity check headers exist.
if [[ ! -f "${INC_DIR}/mqtt/async_client.h" ]]; then
    echo "ERROR: ${INC_DIR}/mqtt/async_client.h missing."
    echo "       Run: brew install eclipse-paho-mqtt-cpp"
    exit 1
fi
if [[ ! -f "${INC_DIR}/nlohmann/json.hpp" ]]; then
    echo "ERROR: ${INC_DIR}/nlohmann/json.hpp missing."
    echo "       Run: brew install nlohmann-json"
    exit 1
fi

echo "Compiling ground_station_manager.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" \
    -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    ground_station_manager.cpp \
    -lpaho-mqttpp3 -lpaho-mqtt3a \
    -o ground_station_manager

echo "Build OK: ./ground_station_manager"
echo
echo "Run with:  ./ground_station_manager"
echo "Stop with: Ctrl-C"
