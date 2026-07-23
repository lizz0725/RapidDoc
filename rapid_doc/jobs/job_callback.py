"""一次性 Job 终态回调的独立 Dispatcher。"""

from __future__ import annotations

import hashlib
import hmac
import threading
from datetime import datetime, timezone
from typing import Protocol

import requests
from loguru import logger

from .job_config import JobSettings
from .job_database import initialize_database
from .job_store import JobStore


class CallbackSender(Protocol):
    """回调网络适配接口，便于无网络单元测试。"""

    def send(
        self,
        callback_url: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: tuple[int, int],
    ) -> int:
        """投递一次回调，返回 HTTP 状态码或抛出网络异常。"""


class RequestsCallbackSender:
    """基于 requests 的同步 HTTP sender；仅由 dispatcher 进程调用。"""

    def send(
        self,
        callback_url: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: tuple[int, int],
    ) -> int:
        response = requests.post(
            callback_url,
            data=payload,
            headers=headers,
            timeout=timeout,
        )
        return response.status_code


class CallbackDispatcher:
    """顺序投递 outbox；先持久化 dispatching，保证每条记录最多实际发起一次请求。"""

    IDLE_WAIT_SECONDS = 1.0

    def __init__(
        self,
        settings: JobSettings,
        *,
        store: JobStore | None = None,
        sender: CallbackSender | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or JobStore(settings, settings)
        self.sender = sender or RequestsCallbackSender()

    def initialize(self) -> None:
        initialize_database(self.settings)

    def run_once(self) -> bool:
        """投递一条 pending 记录；没有待投递记录时返回 False。"""

        self.initialize()
        delivery = self.store.claim_next_callback()
        if delivery is None:
            return False

        delivery_id = str(delivery["delivery_id"])
        payload = str(delivery["payload_json"]).encode("utf-8")
        headers = self._headers(delivery_id, payload)
        try:
            status_code = self.sender.send(
                str(delivery["callback_url_snapshot"]),
                payload,
                headers,
                (
                    self.settings.callback_connect_timeout_seconds,
                    self.settings.callback_read_timeout_seconds,
                ),
            )
        except Exception as exc:
            self.store.complete_callback(
                delivery_id,
                delivered=False,
                http_status=None,
                error_code="CALLBACK_REQUEST_ERROR",
                error_message=str(exc),
            )
            logger.warning("Job 回调 {} 请求失败：{}", delivery_id, exc)
            return True

        if 200 <= status_code < 300:
            self.store.complete_callback(
                delivery_id,
                delivered=True,
                http_status=status_code,
            )
            logger.info("Job 回调 {} 投递成功，HTTP {}。", delivery_id, status_code)
            return True

        self.store.complete_callback(
            delivery_id,
            delivered=False,
            http_status=status_code,
            error_code="CALLBACK_HTTP_STATUS",
            error_message=f"回调接口返回 HTTP {status_code}。",
        )
        logger.warning("Job 回调 {} 返回非成功状态：HTTP {}。", delivery_id, status_code)
        return True

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """供容器启动脚本使用的 dispatcher 主循环。"""

        from .job_runtime import CALLBACK_DISPATCHER_COMPONENT, ServiceHeartbeat

        stop_event = stop_event or threading.Event()
        heartbeat = ServiceHeartbeat(self.settings, CALLBACK_DISPATCHER_COMPONENT)
        heartbeat.start()
        try:
            while not stop_event.is_set():
                if not self.run_once():
                    stop_event.wait(self.IDLE_WAIT_SECONDS)
        finally:
            heartbeat.stop()

    def _headers(self, delivery_id: str, payload: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-RapidDoc-Delivery-Id": delivery_id,
            "X-RapidDoc-Timestamp": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        secret = self.settings.callback_signing_secret
        if secret:
            signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
            headers["X-RapidDoc-Signature"] = f"v1={signature}"
        return headers


def main() -> None:
    """以 ``python -m rapid_doc.jobs.job_callback`` 运行回调 Dispatcher。"""

    CallbackDispatcher(JobSettings.from_env()).run_forever()


if __name__ == "__main__":
    main()
