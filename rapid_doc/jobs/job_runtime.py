"""异步 Job 进程心跳、就绪检查与运行时可观测性。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .job_artifacts import ArtifactStore
from .job_config import JobSettings
from .job_database import connect_database, initialize_database
from .job_limits import JobAdmissionLimits


OCR_WORKER_COMPONENT = "ocr_worker"
MAINTENANCE_COMPONENT = "maintenance"
CALLBACK_DISPATCHER_COMPONENT = "callback_dispatcher"


@dataclass(frozen=True)
class ComponentReadiness:
    """单类后台组件的就绪状态。"""

    required: int
    healthy: int

    @property
    def ready(self) -> bool:
        return self.healthy >= self.required


class JobRuntime:
    """集中处理不属于业务状态机的运行时元数据。"""

    def __init__(self, settings: JobSettings) -> None:
        self.settings = settings
        self.artifacts = ArtifactStore(settings.data_dir)

    def initialize(self) -> None:
        self.artifacts.ensure_layout()
        initialize_database(self.settings.database_path)

    def record_heartbeat(
        self,
        component_type: str,
        component_id: str,
        *,
        state: str = "running",
        details: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> None:
        """写入一条组件心跳；使用 UPSERT 避免进程重启留下重复记录。"""

        self.initialize()
        now = _current_timestamp() if now is None else now
        serialized_details = (
            json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            if details is not None
            else None
        )
        connection = connect_database(self.settings.database_path)
        try:
            connection.execute(
                """
                INSERT INTO service_heartbeats (
                    component_type, component_id, pid, component_state, last_seen_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(component_type, component_id) DO UPDATE SET
                    pid = excluded.pid,
                    component_state = excluded.component_state,
                    last_seen_at = excluded.last_seen_at,
                    details_json = excluded.details_json
                """,
                (
                    component_type,
                    component_id,
                    os.getpid(),
                    state,
                    now,
                    serialized_details,
                ),
            )
        finally:
            connection.close()

    def readiness(self, now: int | None = None) -> dict[str, Any]:
        """返回 API 就绪检查需要的 SQLite、文件和后台进程状态。"""

        now = _current_timestamp() if now is None else now
        checks: dict[str, Any] = {}
        try:
            self.initialize()
            self._probe_data_directory()
            connection = connect_database(self.settings.database_path)
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
            checks["database"] = {"ready": True}
            checks["dataDirectory"] = {"ready": True}
        except (OSError, sqlite3.Error) as exc:
            checks["database"] = {"ready": False, "message": str(exc)}
            checks["dataDirectory"] = {"ready": False, "message": str(exc)}

        try:
            retained_bytes = self.artifacts.retained_bytes()
            storage_ready = retained_bytes < JobAdmissionLimits.MAX_RETAINED_BYTES
            checks["storage"] = {
                "ready": storage_ready,
                "retainedBytes": retained_bytes,
                "maxRetainedBytes": JobAdmissionLimits.MAX_RETAINED_BYTES,
            }
        except OSError as exc:
            checks["storage"] = {"ready": False, "message": str(exc)}
        try:
            components = self._component_readiness(now)
        except (OSError, sqlite3.Error) as exc:
            components = {}
            checks["componentsError"] = {"ready": False, "message": str(exc)}
        checks["components"] = {
            name: {
                "ready": readiness.ready,
                "healthy": readiness.healthy,
                "required": readiness.required,
            }
            for name, readiness in components.items()
        }
        ready = (
            all(
                check.get("ready", False)
                for name, check in checks.items()
                if name != "components"
            )
            and all(component.ready for component in components.values())
        )
        return {"ready": ready, "checks": checks}

    def _component_readiness(self, now: int) -> dict[str, ComponentReadiness]:
        if not self.settings.async_enabled:
            return {}
        required = {
            OCR_WORKER_COMPONENT: self.settings.worker_processes,
            MAINTENANCE_COMPONENT: 1,
            CALLBACK_DISPATCHER_COMPONENT: 1,
        }
        fresh_after = now - self.settings.background_heartbeat_fresh_seconds
        connection = connect_database(self.settings.database_path)
        try:
            counts = {
                row["component_type"]: row["healthy_count"]
                for row in connection.execute(
                    """
                    SELECT component_type, COUNT(*) AS healthy_count
                    FROM service_heartbeats
                    WHERE component_state = ? AND last_seen_at >= ?
                    GROUP BY component_type
                    """,
                    ("running", fresh_after),
                )
            }
        finally:
            connection.close()
        return {
            component_type: ComponentReadiness(
                required=component_required,
                healthy=int(counts.get(component_type, 0)),
            )
            for component_type, component_required in required.items()
        }

    def _probe_data_directory(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".readiness-", dir=self.settings.data_dir
        )
        try:
            os.write(descriptor, b"ready")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(temporary_name).unlink(missing_ok=True)


class ServiceHeartbeat:
    """后台进程的独立心跳线程，OCR 被原生推理阻塞时仍能上报存活。"""

    def __init__(
        self,
        settings: JobSettings,
        component_type: str,
        *,
        component_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.component_type = component_type
        self.component_id = component_id or os.environ.get(
            "RAPID_DOC_COMPONENT_ID", f"{component_type}-{os.getpid()}"
        )
        self.details = details
        self.runtime = JobRuntime(settings)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"rapid-doc-{component_type}-heartbeat",
        )

    def start(self) -> None:
        self._record("running")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self.settings.heartbeat_seconds + 1)
        self._record("stopped")

    def _run(self) -> None:
        while not self._stop_event.wait(self.settings.heartbeat_seconds):
            self._record("running")

    def _record(self, state: str) -> None:
        try:
            self.runtime.record_heartbeat(
                self.component_type,
                self.component_id,
                state=state,
                details=self.details,
            )
        except (OSError, sqlite3.Error):
            # 数据盘暂时不可用时不应让心跳线程终止主进程；ready 会呈现该故障。
            return


def _current_timestamp() -> int:
    return int(time.time())
