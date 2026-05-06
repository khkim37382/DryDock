#!/usr/bin/env bash
# ============================================================
# build_processor.sh — GSM Mac build for ground_station_processor.cpp
# ============================================================
# Auto-detects Homebrew prefix (Apple Silicon / Intel).
#
# The processor reads from disk only — NO broker connection needed.
#
# USAGE:
#   ./build_processor.sh           # build only
#   ./build_processor.sh --run     # build + run immediately
#
# After building:
#   ./ground_station_processor
#
# The processor automatically discovers the most recent session folder
# inside received_transmissions/ and watches its json/ subdirectory.
# Start it AFTER ground_station_manager so the session folder exists.
#
# Output per session:
#   received_transmissions/SESSION/parquet/satellites/OBJECT_ID/batch_*.parquet
#   received_transmissions/SESSION/parquet/satellites/master_index.parquet
#   received_transmissions/SESSION/parquet/asteroids/master_index.parquet
#   received_transmissions/SESSION/parquet/debris/master_index.parquet
#   received_transmissions/SESSION/parquet/summary.parquet
#   received_transmissions/SESSION/json_index.parquet
#
# Dependencies (run once):
#   brew install apache-arrow nlohmann-json
# ============================================================
set -euo pipefail

RUN_AFTER_BUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run) RUN_AFTER_BUILD=true; shift ;;
        *) echo "Unknown arg: $1"; echo "Usage: $0 [--run]"; exit 1 ;;
    esac
done

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew not found. Install from https://brew.sh"; exit 1
fi
BREW_PREFIX="$(brew --prefix)"
echo "Homebrew prefix: ${BREW_PREFIX}"

need_install=()
for pkg in apache-arrow nlohmann-json; do
    brew list --formula --versions "${pkg}" >/dev/null 2>&1 || need_install+=("${pkg}")
done
[[ ${#need_install[@]} -gt 0 ]] && { echo "Installing: ${need_install[*]}"; brew install "${need_install[@]}"; }

INC_DIR="${BREW_PREFIX}/include"
LIB_DIR="${BREW_PREFIX}/lib"

[[ -f "${INC_DIR}/arrow/api.h"            ]] || { echo "ERROR: Arrow missing.   Run: brew install apache-arrow"; exit 1; }
[[ -f "${INC_DIR}/parquet/arrow/writer.h" ]] || { echo "ERROR: Parquet missing. Run: brew install apache-arrow"; exit 1; }
[[ -f "${INC_DIR}/nlohmann/json.hpp"      ]] || { echo "ERROR: nlohmann missing. Run: brew install nlohmann-json"; exit 1; }

echo "Compiling ground_station_processor.cpp (C++20, -O2) ..."
clang++ -std=c++20 -O2 -pthread -Wall -Wextra -Wno-unused-parameter \
    -I"${INC_DIR}" -L"${LIB_DIR}" \
    -Wl,-rpath,"${LIB_DIR}" \
    ground_station_processor.cpp \
    -larrow -lparquet \
    -o ground_station_processor

echo "Build OK: ./ground_station_processor"
echo ""
echo "Run with:  ./ground_station_processor"
echo "NOTE: Start AFTER ground_station_manager (needs session folder to exist)."
echo "NOTE: No broker flag needed — reads from disk only."

if [[ "${RUN_AFTER_BUILD}" == true ]]; then
    echo ""
    echo "Starting: ./ground_station_processor"
    exec ./ground_station_processor
fi
