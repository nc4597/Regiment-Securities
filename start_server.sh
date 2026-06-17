#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
PID_FILE="uvicorn-${PORT}.pid"
LOG_FILE="uvicorn-${PORT}.log"

if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "Server already running at http://${HOST}:${PORT} (PID ${PID})"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

nohup venv/bin/uvicorn server:app --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
PID="$!"
echo "$PID" > "$PID_FILE"

sleep 1

if kill -0 "$PID" 2>/dev/null; then
    echo "Server running at http://${HOST}:${PORT} (PID ${PID})"
    echo "Logs: ${LOG_FILE}"
else
    echo "Server failed to start. Logs:"
    sed -n '1,120p' "$LOG_FILE"
    exit 1
fi
