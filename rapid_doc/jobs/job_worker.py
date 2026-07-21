"""独立 OCR Worker：领取 FIFO owner、续租、解析并发布共享结果。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from .job_artifacts import ArtifactStore
from .job_config import JobSettings
from .job_database import initialize_database
from .job_parser import JobParseError, ParsedDocument, RapidDocParseAdapter
from .job_store import JobStore
from .job_types import generate_ulid


class JobParser(Protocol):
    def parse(self, job: dict[str, Any], input_path: Path, output_dir: Path) -> ParsedDocument:
        """返回要发布的 Markdown 及少量固定解析元数据。"""


class JobWorker:
    """单个 OS 进程内串行执行 OCR；并发度由启动多个此类进程决定。"""

    IDLE_WAIT_SECONDS = 1.0

    def __init__(
        self,
        settings: JobSettings,
        *,
        store: JobStore | None = None,
        artifacts: ArtifactStore | None = None,
        parser: JobParser | None = None,
    ) -> None:
        self.settings = settings
        self.artifacts = artifacts or ArtifactStore(settings.data_dir)
        self.store = store or JobStore(settings.database_path, settings)
        self.parser = parser or RapidDocParseAdapter()

    def initialize(self) -> None:
        self.artifacts.ensure_layout()
        initialize_database(self.settings.database_path)

    def run_once(self) -> bool:
        """处理一个 owner；没有可领取任务时返回 False。"""

        self.initialize()
        attempt_token = generate_ulid()
        job = self.store.claim_next_job(attempt_token)
        if job is None:
            return False

        heartbeat = _LeaseHeartbeat(self.store, job["job_id"], attempt_token, self.settings)
        publishing_started = False
        heartbeat.start()
        try:
            input_path = self.artifacts.input_path(job["job_id"], job["stored_filename"])
            if not input_path.is_file():
                raise FileNotFoundError(input_path)
            attempt_dir = self.artifacts.attempt_result_path(
                job["job_id"], attempt_token
            ).parent
            parsed = self.parser.parse(job, input_path, attempt_dir / "rapid_doc")
            if heartbeat.lease_lost:
                logger.warning("Job {} 的租约已失效，保留结果由维护进程恢复。", job["job_id"])
                return True

            result_payload = self._result_payload(job, parsed)
            temporary_path = self.artifacts.attempt_result_path(job["job_id"], attempt_token)
            self.artifacts.write_bytes_atomic(temporary_path, result_payload)
            if not self.store.begin_publishing(job["job_id"], attempt_token):
                logger.warning("Job {} 无法进入发布阶段，保留 attempt 结果供恢复。", job["job_id"])
                return True
            publishing_started = True

            json_path = self.artifacts.cache_result_path(
                job["tenant_id"], job["source_sha256"], "json"
            )
            markdown_path = self.artifacts.cache_result_path(
                job["tenant_id"], job["source_sha256"], "md"
            )
            self.artifacts.write_bytes_atomic(markdown_path, parsed.markdown.encode("utf-8"))
            self.artifacts.publish(temporary_path, json_path)
            result_path = str(json_path.relative_to(self.settings.data_dir))
            if not self.store.complete_publishing(
                job["job_id"], attempt_token, result_path, len(result_payload)
            ):
                logger.warning("Job {} 的结果文件已发布，但数据库状态等待恢复。", job["job_id"])
            else:
                logger.info("Job {} 已完成 OCR 并发布共享结果。", job["job_id"])
            return True
        except FileNotFoundError:
            self._fail_running_job(job, attempt_token, "STORAGE_INPUT_MISSING", "任务原始文件不存在。")
            return True
        except JobParseError as exc:
            self._fail_running_job(job, attempt_token, "OCR_PARSE_FAILED", str(exc))
            return True
        except Exception as exc:
            if publishing_started:
                logger.exception("Job {} 发布异常，等待维护进程恢复：{}", job["job_id"], exc)
            else:
                self._fail_running_job(job, attempt_token, "OCR_UNEXPECTED_ERROR", str(exc))
            return True
        finally:
            heartbeat.stop()

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """供容器启动脚本使用的进程主循环。"""

        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            if not self.run_once():
                stop_event.wait(self.IDLE_WAIT_SECONDS)

    def _fail_running_job(
        self, job: dict[str, Any], attempt_token: str, error_code: str, error_message: str
    ) -> None:
        if self.store.fail_owner_job(job["job_id"], attempt_token, error_code, error_message):
            logger.warning("Job {} 解析失败：{}", job["job_id"], error_message)
        else:
            logger.warning("Job {} 已不归当前 Worker 所有，跳过失败写入。", job["job_id"])

    @staticmethod
    def _result_payload(job: dict[str, Any], parsed: ParsedDocument) -> bytes:
        return json.dumps(
            {
                "markdown": parsed.markdown,
                "metadata": {
                    **parsed.metadata,
                    "sourceFilename": job["source_filename"],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


class _LeaseHeartbeat:
    """解析期间由轻量线程续租，不让耗时 OCR 占用 SQLite 写锁。"""

    def __init__(
        self, store: JobStore, job_id: str, attempt_token: str, settings: JobSettings
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.attempt_token = attempt_token
        self.settings = settings
        self.lease_lost = False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"job-lease-{job_id}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self.settings.heartbeat_seconds + 1)

    def _run(self) -> None:
        while not self._stop_event.wait(self.settings.heartbeat_seconds):
            if not self.store.renew_lease(self.job_id, self.attempt_token):
                self.lease_lost = True
                return


def main() -> None:
    """以 ``python -m rapid_doc.jobs.job_worker`` 运行单个 OCR Worker。"""

    JobWorker(JobSettings.from_env()).run_forever()


if __name__ == "__main__":
    main()
