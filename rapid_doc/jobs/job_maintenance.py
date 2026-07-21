"""异步 Job 的看门狗与清理进程。

本进程不执行 OCR。它只在短事务中收敛失联任务、恢复已落盘的发布结果，并在数据库
明确允许后删除文件。这样即使容器异常退出，后续重启也不会让一个 Job 永久挡住 FIFO。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .job_artifacts import ArtifactStore
from .job_config import JobSettings
from .job_database import initialize_database
from .job_store import JobStore


@dataclass(frozen=True)
class MaintenanceReport:
    """单轮维护的简要计数，便于测试与运维日志聚合。"""

    publishing_recovered: int = 0
    expired_leases: int = 0
    run_timeouts: int = 0
    queue_expired: int = 0
    result_expired: int = 0
    caches_removed: int = 0
    jobs_purged: int = 0
    staging_removed: int = 0


class JobMaintenance:
    """协调 Watchdog 与 Sweeper；所有 OCR 结果读取均由 ArtifactStore 约束路径。"""

    def __init__(
        self,
        settings: JobSettings,
        *,
        store: JobStore | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.settings = settings
        self.artifacts = artifacts or ArtifactStore(settings.data_dir)
        self.store = store or JobStore(settings.database_path, settings)

    def initialize(self) -> None:
        self.artifacts.ensure_layout()
        initialize_database(self.settings.database_path)

    def run_watchdog_once(self, now: int | None = None) -> MaintenanceReport:
        """恢复发布中任务、回收失效租约，并标记运行超时任务。"""

        now = _current_timestamp() if now is None else now
        self.initialize()
        publishing_recovered = self._recover_publishing_jobs(now)
        run_timeouts = self.store.fail_overlong_running_jobs(now)
        expired_leases = self.store.recover_expired_running_jobs(now)
        report = MaintenanceReport(
            publishing_recovered=publishing_recovered,
            expired_leases=expired_leases,
            run_timeouts=run_timeouts,
        )
        self._log_report("Watchdog", report)
        return report

    def run_sweeper_once(self, now: int | None = None) -> MaintenanceReport:
        """处理排队、结果、缓存与墓碑的保留期，并删除其对应文件。"""

        now = _current_timestamp() if now is None else now
        self.initialize()
        queue_expired = self.store.expire_queued_jobs(now)
        result_expired = self.store.expire_job_results(now)
        removed_caches = self.store.remove_expired_caches(now)
        for cache in removed_caches:
            self.artifacts.remove_cache_artifacts(cache["tenant_id"], cache["source_sha256"])
        purged_job_ids = self.store.purge_expired_tombstones(now)
        for job_id in purged_job_ids:
            self.artifacts.remove_job_artifacts(job_id)
        staging_removed = self.artifacts.remove_stale_staging(now - self.settings.queue_expire_seconds)
        report = MaintenanceReport(
            queue_expired=queue_expired,
            result_expired=result_expired,
            caches_removed=len(removed_caches),
            jobs_purged=len(purged_job_ids),
            staging_removed=staging_removed,
        )
        self._log_report("Sweeper", report)
        return report

    def run_once(self, now: int | None = None) -> MaintenanceReport:
        """测试和手工运维使用的一次完整维护轮次。"""

        now = _current_timestamp() if now is None else now
        watchdog = self.run_watchdog_once(now)
        sweeper = self.run_sweeper_once(now)
        return MaintenanceReport(
            publishing_recovered=watchdog.publishing_recovered,
            expired_leases=watchdog.expired_leases,
            run_timeouts=watchdog.run_timeouts,
            queue_expired=sweeper.queue_expired,
            result_expired=sweeper.result_expired,
            caches_removed=sweeper.caches_removed,
            jobs_purged=sweeper.jobs_purged,
            staging_removed=sweeper.staging_removed,
        )

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """以不同间隔运行 Watchdog 和 Sweeper，供容器启动脚本拉起。"""

        stop_event = stop_event or threading.Event()
        next_sweeper_at = 0.0
        while not stop_event.is_set():
            current = time.monotonic()
            self.run_watchdog_once()
            if current >= next_sweeper_at:
                self.run_sweeper_once()
                next_sweeper_at = current + self.settings.sweeper_interval_seconds
            stop_event.wait(self.settings.watchdog_interval_seconds)

    def _recover_publishing_jobs(self, now: int) -> int:
        recovered = 0
        for job in self.store.list_publishing_jobs():
            if self._recover_publishing_job(job, now):
                recovered += 1
        return recovered

    def _recover_publishing_job(self, job: dict[str, Any], now: int) -> bool:
        job_id = str(job["job_id"])
        attempt_token = str(job["active_attempt_token"])
        temporary_path = self.artifacts.attempt_result_path(job_id, attempt_token)
        json_path = self.artifacts.cache_result_path(
            str(job["tenant_id"]), str(job["source_sha256"]), "json"
        )
        markdown_path = self.artifacts.cache_result_path(
            str(job["tenant_id"]), str(job["source_sha256"]), "md"
        )
        relative_result_path = str(json_path.relative_to(self.artifacts.root))

        try:
            if not json_path.exists() and temporary_path.exists():
                payload = _read_result_payload(temporary_path)
                self.artifacts.write_bytes_atomic(
                    markdown_path, str(payload["markdown"]).encode("utf-8")
                )
                self.artifacts.publish(temporary_path, json_path)
            if json_path.exists():
                payload = self.artifacts.read_result_json(relative_result_path)
                if not markdown_path.exists():
                    self.artifacts.write_bytes_atomic(
                        markdown_path, str(payload["markdown"]).encode("utf-8")
                    )
                completed = self.store.complete_publishing(
                    job_id,
                    attempt_token,
                    relative_result_path,
                    json_path.stat().st_size,
                    now,
                )
                if completed:
                    logger.info("维护进程已恢复 Job {} 的已落盘结果。", job_id)
                return completed
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Job {} 的发布结果无法恢复：{}", job_id, exc)

        requeued = self.store.requeue_incomplete_publishing(job_id, attempt_token, now)
        if requeued:
            logger.warning("Job {} 的发布文件不完整，已按重试策略收敛。", job_id)
        return requeued

    @staticmethod
    def _log_report(component: str, report: MaintenanceReport) -> None:
        values = {name: value for name, value in report.__dict__.items() if value}
        if values:
            logger.info("{} 本轮维护完成：{}", component, values)


def _read_result_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
        raise ValueError("attempt result does not contain markdown")
    return payload


def _current_timestamp() -> int:
    return int(time.time())


def main() -> None:
    """以 ``python -m rapid_doc.jobs.job_maintenance`` 运行维护进程。"""

    JobMaintenance(JobSettings.from_env()).run_forever()


if __name__ == "__main__":
    main()
