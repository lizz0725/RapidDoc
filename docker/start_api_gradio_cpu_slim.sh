#!/bin/bash
set -euo pipefail

if [ -f "/app/.env" ]; then
    echo "加载 /app/.env 中的环境变量。"
    set -a
    # shellcheck disable=SC1091
    . "/app/.env"
    set +a
fi

export PYTHONPATH="${PYTHONPATH:-/app}"
export API_PORT="${API_PORT:-8888}"
export GRADIO_SERVER_NAME="${GRADIO_SERVER_NAME:-0.0.0.0}"
export GRADIO_SERVER_PORT="${GRADIO_SERVER_PORT:-7860}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export RAPID_DOC_JOB_DATA_DIR="${RAPID_DOC_JOB_DATA_DIR:-/app/output/jobs}"

if [ -f "/opt/rapid-doc/release/app.py" ]; then
    APP_PATH="/opt/rapid-doc/release/app.py"
else
    APP_PATH="/app/app.py"
fi

is_enabled() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

ASYNC_ENABLED="${RAPID_DOC_ASYNC_ENABLED:-true}"
WORKER_PROCESSES="${RAPID_DOC_WORKER_PROCESSES:-1}"

if ! [[ "${WORKER_PROCESSES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "RAPID_DOC_WORKER_PROCESSES 必须是正整数。" >&2
    exit 64
fi

declare -A PID_BY_NAME=()
SHUTTING_DOWN=0

start_component() {
    local name="$1"
    echo "启动组件=${name}"
    case "${name}" in
        api)
            python3 "${APP_PATH}" &
            ;;
        gradio)
            python3 -m rapid_doc.cli.gradio_app \
                --server-name "${GRADIO_SERVER_NAME}" \
                --server-port "${GRADIO_SERVER_PORT}" &
            ;;
        maintenance)
            RAPID_DOC_COMPONENT_ID="maintenance-main" \
                python3 -m rapid_doc.jobs.job_maintenance &
            ;;
        callback-dispatcher)
            RAPID_DOC_COMPONENT_ID="callback-dispatcher-main" \
                python3 -m rapid_doc.jobs.job_callback &
            ;;
        ocr-worker-*)
            RAPID_DOC_COMPONENT_ID="${name}" \
                python3 -m rapid_doc.jobs.job_worker &
            ;;
        *)
            echo "未知组件=${name}" >&2
            return 64
            ;;
    esac
    PID_BY_NAME["${name}"]=$!
    echo "组件=${name} pid=${PID_BY_NAME[${name}]} 已启动。"
}

start_all_components() {
    start_component api
    start_component gradio
    if is_enabled "${ASYNC_ENABLED}"; then
        local index
        for ((index = 1; index <= WORKER_PROCESSES; index++)); do
            start_component "ocr-worker-${index}"
        done
        start_component maintenance
        start_component callback-dispatcher
    else
        echo "异步 Job 已禁用，仅启动原有 API 与 Gradio。"
    fi
}

terminate_workers_for_timeout() {
    local name pid
    local signal_path="${RAPID_DOC_JOB_DATA_DIR}/control/restart-workers.request"
    [ -f "${signal_path}" ] || return 0

    echo "检测到 OCR 超时重启请求，终止当前全部 OCR Worker。"
    for name in "${!PID_BY_NAME[@]}"; do
        if [[ "${name}" == ocr-worker-* ]]; then
            pid="${PID_BY_NAME[${name}]}"
            kill -TERM "${pid}" 2>/dev/null || true
        fi
    done
    rm -f "${signal_path}"
}

reap_and_restart_components() {
    local name pid exit_code
    for name in "${!PID_BY_NAME[@]}"; do
        pid="${PID_BY_NAME[${name}]}"
        if kill -0 "${pid}" 2>/dev/null; then
            continue
        fi
        if wait "${pid}"; then
            exit_code=0
        else
            exit_code=$?
        fi
        unset 'PID_BY_NAME[$name]'
        if [ "${SHUTTING_DOWN}" -eq 1 ]; then
            continue
        fi
        echo "组件=${name} pid=${pid} 已退出，exitCode=${exit_code}；1 秒后重启。" >&2
        sleep 1
        start_component "${name}"
    done
}

shutdown() {
    SHUTTING_DOWN=1
    echo "收到停止信号，正在终止 RapidDoc 子进程。"
    local pid
    for pid in "${PID_BY_NAME[@]}"; do
        kill -TERM "${pid}" 2>/dev/null || true
    done
    for pid in "${PID_BY_NAME[@]}"; do
        wait "${pid}" 2>/dev/null || true
    done
    exit 0
}

trap shutdown INT TERM

echo "RapidDoc CPU slim image"
echo "API:    http://localhost:${API_PORT}"
echo "Gradio: http://localhost:${GRADIO_SERVER_PORT}"
echo "异步 Job: ${ASYNC_ENABLED}，OCR Worker 数量: ${WORKER_PROCESSES}"
echo "公式识别在此镜像中默认关闭。"
echo "======================================"

cd /app
start_all_components

while true; do
    if is_enabled "${ASYNC_ENABLED}"; then
        terminate_workers_for_timeout
    fi
    reap_and_restart_components
    sleep 1
done
