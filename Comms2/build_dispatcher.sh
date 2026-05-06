#!/usr/bin/env bash
# ============================================================
# build_dispatcher.sh — SMS Mac build for dispatcher.cpp
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
for pkg in libpaho-mqtt nlohmann-json cmake; do
    brew list --formula --versions "${pkg}" >/dev/null 2>&1 || need_install+=("${pkg}")
done
[[ ${#need_install[@]} -gt 0 ]] && { echo "Installing: ${need_install[*]}"; brew install "${need_install[@]}"; }

INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

[[ -f "${INC_DIR}/MQTTAsync.h" ]] || { echo "ERROR: paho C missing. Run: brew install libpaho-mqtt"; exit 1; }
[[ -f "${INC_DIR}/nlohmann/json.hpp" ]] || { echo "ERROR: nlohmann missing. Run: brew install nlohmann-json"; exit 1; }

# Download and compile the C++ wrapper if missing
if [ ! -f "${INC_DIR}/mqtt/async_client.h" ]; then
    echo "============================================================"
    echo " Paho MQTT C++ wrapper not found. Building from source..."
    echo "============================================================"
    rm -rf /tmp/paho.mqtt.cpp
    git clone https://github.com/eclipse/paho.mqtt.cpp.git /tmp/paho.mqtt.cpp
    cd /tmp/paho.mqtt.cpp
    cmake -Bbuild -H. -DPAHO_BUILD_DOCUMENTATION=OFF -DPAHO_BUILD_SAMPLES=OFF -DCMAKE_PREFIX_PATH="${BREW_PREFIX}" -DCMAKE_INSTALL_PREFIX="${BREW_PREFIX}"
    cmake --build build/ --target install
    cd -
    rm -rf /tmp/paho.mqtt.cpp
    echo "Paho MQTT C++ installation complete."
fi

echo "Compiling dispatcher.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    dispatcher.cpp \
    -lpaho-mqttpp3 -lpaho-mqtt3a \
    -o dispatcher

echo "Build OK: ./dispatcher"

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