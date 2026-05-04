#!/usr/bin/env bash
# ============================================================
# build_dispatcher.sh — macOS build script for dispatcher.cpp
# ============================================================
# Auto-detects Homebrew prefix:
#   /opt/homebrew  on Apple Silicon (M1/M2/M3/M4)
#   /usr/local     on Intel Macs
#
# Dependencies (install once):
#   brew install eclipse-paho-mqtt-cpp nlohmann-json
#
# Usage:
#   chmod +x build_dispatcher.sh
#   ./build_dispatcher.sh
# ============================================================
set -euo pipefail

# ── Detect Homebrew prefix ───────────────────────────────────
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

# ── Header & lib paths ──────────────────────────────────────
INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

# Sanity check Paho headers exist
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

# ── Compile ─────────────────────────────────────────────────
echo "Compiling dispatcher.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" \
    -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    dispatcher.cpp \
    -lpaho-mqttpp3 -lpaho-mqtt3a \
    -o dispatcher

echo "Build OK: ./dispatcher"
echo
echo "Run with:  ./dispatcher"
echo "Stop with: Ctrl-C"
