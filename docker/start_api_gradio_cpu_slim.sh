#!/bin/bash
set -euo pipefail

if [ -f "/app/.env" ]; then
    echo "Loading environment variables from .env file..."
    export $(grep -v '^#' /app/.env | xargs)
fi

export PYTHONPATH=/app
export API_PORT=${API_PORT:-8888}
export GRADIO_SERVER_NAME=${GRADIO_SERVER_NAME:-0.0.0.0}
export GRADIO_SERVER_PORT=${GRADIO_SERVER_PORT:-7860}
export LOG_LEVEL=${LOG_LEVEL:-INFO}

echo "RapidDoc CPU slim image"
echo "API:    http://localhost:${API_PORT}"
echo "Gradio: http://localhost:${GRADIO_SERVER_PORT}"
echo "Formula recognition defaults to disabled in this image."
echo "======================================"

cd /app

python3 app.py &
api_pid=$!

python3 -m rapid_doc.cli.gradio_app \
    --server-name "${GRADIO_SERVER_NAME}" \
    --server-port "${GRADIO_SERVER_PORT}" &
gradio_pid=$!

trap 'kill "${api_pid}" "${gradio_pid}" 2>/dev/null || true' INT TERM

wait -n "${api_pid}" "${gradio_pid}"
exit_code=$?
kill "${api_pid}" "${gradio_pid}" 2>/dev/null || true
wait "${api_pid}" "${gradio_pid}" 2>/dev/null || true
exit "${exit_code}"
