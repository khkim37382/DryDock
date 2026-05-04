#!/usr/bin/env bash
# ============================================================
# build_processor.sh — macOS build for ground_station_processor.cpp
# ============================================================
# Auto-detects Homebrew prefix:
#   /opt/homebrew  on Apple Silicon (M1/M2/M3/M4)
#   /usr/local     on Intel Macs
#
# Dependencies (install once):
#   brew install apache-arrow nlohmann-json
#
# Usage:
#   chmod +x build_processor.sh
#   ./build_processor.sh
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
for pkg in apache-arrow nlohmann-json; do
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
if [[ ! -f "${INC_DIR}/arrow/api.h" ]]; then
    echo "ERROR: ${INC_DIR}/arrow/api.h missing."
    echo "       Run: brew install apache-arrow"
    exit 1
fi
if [[ ! -f "${INC_DIR}/parquet/arrow/writer.h" ]]; then
    echo "ERROR: ${INC_DIR}/parquet/arrow/writer.h missing."
    echo "       Run: brew install apache-arrow"
    exit 1
fi
if [[ ! -f "${INC_DIR}/nlohmann/json.hpp" ]]; then
    echo "ERROR: ${INC_DIR}/nlohmann/json.hpp missing."
    echo "       Run: brew install nlohmann-json"
    exit 1
fi

echo "Compiling ground_station_processor.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" \
    -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    ground_station_processor.cpp \
    -larrow -lparquet \
    -o ground_station_processor

echo "Build OK: ./ground_station_processor"
echo
echo "Run with:  ./ground_station_processor"
echo "Stop with: Ctrl-C"
