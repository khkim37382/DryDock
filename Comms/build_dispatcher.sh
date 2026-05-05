#!/usr/bin/env bash
# ============================================================
# build_dispatcher.sh — SMS Mac build for dispatcher.cpp
# ============================================================
# Auto-detects Homebrew prefix (Apple Silicon: /opt/homebrew,
# Intel Mac: /usr/local).
#
# USAGE:
#   ./build_dispatcher.sh                               # build only
#   ./build_dispatcher.sh --run                         # build + run (localhost)
#   ./build_dispatcher.sh --run --broker 192.168.1.50   # build + run (remote)
#   ./build_dispatcher.sh --run --broker tcp://192.168.1.50:1883
#
# After building, run directly:
#   ./dispatcher
#   ./dispatcher --broker tcp://192.168.1.50:1883
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

# Install missing deps
need_install=()
for pkg in eclipse-paho-mqtt-cpp nlohmann-json; do
    brew list --formula --versions "${pkg}" >/dev/null 2>&1 || need_install+=("${pkg}")
done
[[ ${#need_install[@]} -gt 0 ]] && { echo "Installing: ${need_install[*]}"; brew install "${need_install[@]}"; }

INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

[[ -f "${INC_DIR}/mqtt/async_client.h" ]] || { echo "ERROR: paho missing. Run: brew install eclipse-paho-mqtt-cpp"; exit 1; }
[[ -f "${INC_DIR}/nlohmann/json.hpp"   ]] || { echo "ERROR: nlohmann missing. Run: brew install nlohmann-json";     exit 1; }

echo "Compiling dispatcher.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    dispatcher.cpp \
    -lpaho-mqttpp3 -lpaho-mqtt3a \
    -o dispatcher

echo "Build OK: ./dispatcher"
echo ""
echo "Run examples:"
echo "  ./dispatcher"
echo "  ./dispatcher --broker tcp://192.168.1.50:1883"

if [[ "${RUN_AFTER_BUILD}" == true ]]; then
    echo ""
    if [[ -n "${BROKER_ARG}" ]]; then
        echo "Starting: ./dispatcher --broker ${BROKER_ARG}"
        exec ./dispatcher --broker "${BROKER_ARG}"
    else
        echo "Starting: ./dispatcher"
        exec ./dispatcher
    fi
fi
