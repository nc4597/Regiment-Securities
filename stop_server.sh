#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8001}"
PID_FILE="uvicorn-${PORT}.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found for port ${PORT}."
    exit 0
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped server on port ${PORT} (PID ${PID})"
else
    echo "Server process was not running."
fi

rm -f "$PID_FILE"
