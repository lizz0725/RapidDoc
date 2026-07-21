"""异步 Job 状态的事务化持久层。

Job 子系统中只有本模块可以执行改变状态的 SQL。OCR、回调和维护操作必须在这里的
短事务之外执行，避免长时间持有 SQLite 写锁。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .job_config import JobSettings
from .job_database import connect_database
from .job_limits import JobAdmissionLimits
from .job_types import CallbackState, CacheRole, CacheState, JobState, generate_ulid


class IdempotencyConflictError(Exception):
    """同一租户使用同一幂等键提交了不同请求。"""


class QueueCapacityError(Exception):
    """第一期固定上限的 FIFO 队列已满。"""


@dataclass(frozen=True)
class JobSubmission:
    tenant_id: str
    request_fingerprint: str
    source_filename: str
    stored_filename: str
    source_sha256: str
    source_bytes: int
    business_ref: str | None = None
    callback_url: str | None = None
    idempotency_key: str | None = None
    source_page_count: int | None = None
    processed_page_count: int | None = None
    truncated: bool = False
    warnings: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class JobCreation:
    job: dict[str, Any]
    reused_idempotency_key: bool = False


@dataclass(frozen=True)
class JobCancellation:
    """取消结果：不存在时 job 为 None，不可取消时 cancelled 为 False。"""

    job: dict[str, Any] | None
    cancelled: bool


_MUTABLE_JOB_COLUMNS = frozenset(
    {
        "active_attempt_token",
        "lease_expires_at",
        "result_path",
        "result_expires_at",
        "tombstone_expires_at",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
        "warnings_json",
    }
)

_CALLBACK_TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.EXPIRED.value,
    }
)


class JobStore:
    def __init__(self, database_path: Path, settings: JobSettings) -> None:
        self.database_path = database_path
        self.settings = settings

    def create_or_reuse_job(
        self,
        submission: JobSubmission,
        now: int | None = None,
        job_id: str | None = None,
    ) -> JobCreation:
        now = _current_timestamp() if now is None else now
        idempotency_key_hash = hash_idempotency_key(submission.idempotency_key)
        warnings_json = (
            json.dumps(submission.warnings, ensure_ascii=False, separators=(",", ":"))
            if submission.warnings
            else None
        )

        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key_hash is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE tenant_id = ? AND idempotency_key_hash = ?
                    """,
                    (submission.tenant_id, idempotency_key_hash),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != submission.request_fingerprint:
                        raise IdempotencyConflictError(
                            "idempotency key was already used for a different request"
                        )
                    connection.commit()
                    return JobCreation(dict(existing), reused_idempotency_key=True)

            cache_row = connection.execute(
                """
                SELECT * FROM parse_cache
                WHERE tenant_id = ? AND source_sha256 = ?
                """,
                (submission.tenant_id, submission.source_sha256),
            ).fetchone()
            if cache_row is not None and cache_row["expires_at"] <= now:
                connection.execute(
                    "DELETE FROM parse_cache WHERE tenant_id = ? AND source_sha256 = ?",
                    (submission.tenant_id, submission.source_sha256),
                )
                cache_row = None

            job_id = generate_ulid() if job_id is None else job_id
            if cache_row is not None and cache_row["cache_state"] == CacheState.READY.value:
                job = self._insert_job(
                    connection,
                    job_id=job_id,
                    submission=submission,
                    idempotency_key_hash=idempotency_key_hash,
                    now=now,
                    warnings_json=warnings_json,
                    job_state=JobState.SUCCEEDED,
                    cache_role=CacheRole.HIT,
                    result_path=cache_row["result_path"],
                    result_expires_at=now + self.settings.result_ttl_seconds,
                    finished_at=now,
                )
                connection.execute(
                    """
                UPDATE parse_cache SET last_accessed_at = ?, expires_at = ?
                WHERE tenant_id = ? AND source_sha256 = ?
                """,
                    (
                        now,
                        now + self.settings.cache_ttl_seconds,
                        submission.tenant_id,
                        submission.source_sha256,
                    ),
                )
                self._enqueue_terminal_callback(connection, job_id, now)
            elif cache_row is not None:
                job = self._insert_job(
                    connection,
                    job_id=job_id,
                    submission=submission,
                    idempotency_key_hash=idempotency_key_hash,
                    now=now,
                    warnings_json=warnings_json,
                    job_state=JobState.WAITING_FOR_RESULT,
                    cache_role=CacheRole.FOLLOWER,
                )
            else:
                self._ensure_queue_capacity(connection)
                queue_seq = self._next_queue_seq(connection)
                job = self._insert_job(
                    connection,
                    job_id=job_id,
                    submission=submission,
                    idempotency_key_hash=idempotency_key_hash,
                    now=now,
                    warnings_json=warnings_json,
                    job_state=JobState.QUEUED,
                    cache_role=CacheRole.OWNER,
                    queue_seq=queue_seq,
                )
                connection.execute(
                    """
                    INSERT INTO parse_cache (
                        tenant_id, source_sha256, cache_state, owner_job_id,
                        created_at, last_accessed_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission.tenant_id,
                        submission.source_sha256,
                        CacheState.PROCESSING.value,
                        job_id,
                        now,
                        now,
                        now + self.settings.cache_ttl_seconds,
                    ),
                )

            connection.commit()
            return JobCreation(job)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_job(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        connection = connect_database(self.database_path)
        try:
            row = self._select_job(connection, tenant_id, job_id)
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def get_job_status(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        """读取任务及瞬时队列观察值，不对排队位置作预约承诺。"""

        connection = connect_database(self.database_path)
        try:
            row = self._select_job(connection, tenant_id, job_id)
            if row is None:
                return None
            job = dict(row)
            if job["job_state"] == JobState.QUEUED.value:
                ahead_queued_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE job_state = ? AND cache_role = ? AND queue_seq < ?
                    """,
                    (JobState.QUEUED.value, CacheRole.OWNER.value, job["queue_seq"]),
                ).fetchone()[0]
                queue_position = ahead_queued_count + 1
            else:
                ahead_queued_count = 0
                queue_position = None
            running_job_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_state = ?",
                (JobState.RUNNING.value,),
            ).fetchone()[0]
            job.update(
                queue_position=queue_position,
                ahead_queued_count=ahead_queued_count,
                running_job_count=running_job_count,
            )
            return job
        finally:
            connection.close()

    def get_queue_snapshot(self) -> dict[str, Any]:
        """返回运维接口所需的真实 OCR FIFO 快照，不混入 follower 或终态任务。"""

        connection = connect_database(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT job_id, tenant_id, source_filename, submitted_at
                FROM jobs
                WHERE job_state = ? AND cache_role = ?
                ORDER BY queue_seq ASC
                """,
                (JobState.QUEUED.value, CacheRole.OWNER.value),
            ).fetchall()
            running_job_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_state = ?",
                (JobState.RUNNING.value,),
            ).fetchone()[0]
            return {
                "items": [
                    {
                        "queue_position": position,
                        "job_id": row["job_id"],
                        "tenant_id": row["tenant_id"],
                        "source_filename": row["source_filename"],
                        "submitted_at": row["submitted_at"],
                    }
                    for position, row in enumerate(rows, start=1)
                ],
                "running_job_count": running_job_count,
            }
        finally:
            connection.close()

    def cancel_job(
        self, tenant_id: str, job_id: str, now: int | None = None
    ) -> JobCancellation:
        """取消未开始任务；取消 owner 时把最早 follower 原子提升为新 owner。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, tenant_id, job_id)
            if row is None:
                connection.commit()
                return JobCancellation(job=None, cancelled=False)
            job = dict(row)
            if job["job_state"] not in {
                JobState.QUEUED.value,
                JobState.WAITING_FOR_RESULT.value,
            }:
                connection.commit()
                return JobCancellation(job=job, cancelled=False)

            updated = connection.execute(
                """
                UPDATE jobs SET job_state = ?, finished_at = ?, tombstone_expires_at = ?
                WHERE tenant_id = ? AND job_id = ? AND job_state = ?
                """,
                (
                    JobState.CANCELLED.value,
                    now,
                    now + self.settings.tombstone_ttl_seconds,
                    tenant_id,
                    job_id,
                    job["job_state"],
                ),
            )
            if updated.rowcount != 1:
                connection.commit()
                return JobCancellation(job=job, cancelled=False)

            if job["job_state"] == JobState.QUEUED.value and job["cache_role"] == CacheRole.OWNER.value:
                self._promote_follower_or_clear_cache(connection, job, now)

            self._enqueue_terminal_callback(connection, job_id, now)

            cancelled = self._select_job(connection, tenant_id, job_id)
            connection.commit()
            return JobCancellation(job=dict(cancelled), cancelled=True)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next_job(
        self, attempt_token: str, now: int | None = None
    ) -> dict[str, Any] | None:
        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_state = ? AND cache_role = ?
                ORDER BY queue_seq ASC
                LIMIT 1
                """,
                (JobState.QUEUED.value, CacheRole.OWNER.value),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            updated = connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, processing_attempt = processing_attempt + 1,
                    active_attempt_token = ?, lease_expires_at = ?,
                    started_at = ?
                WHERE job_id = ? AND job_state = ? AND cache_role = ?
                  AND processing_attempt < ?
                """,
                (
                    JobState.RUNNING.value,
                    attempt_token,
                    now + self.settings.lease_seconds,
                    now,
                    row["job_id"],
                    JobState.QUEUED.value,
                    CacheRole.OWNER.value,
                    self.settings.max_processing_attempts,
                ),
            )
            if updated.rowcount != 1:
                connection.commit()
                return None

            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            connection.commit()
            return dict(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(
        self, job_id: str, attempt_token: str, now: int | None = None
    ) -> bool:
        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            updated = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?
                WHERE job_id = ? AND job_state = ? AND active_attempt_token = ?
                """,
                (now + self.settings.lease_seconds, job_id, JobState.RUNNING.value, attempt_token),
            )
            return updated.rowcount == 1
        finally:
            connection.close()

    def begin_publishing(self, job_id: str, attempt_token: str) -> bool:
        """确认本次 attempt 仍持有任务后，切换到文件发布阶段。"""

        return self.transition_job(
            job_id,
            JobState.RUNNING,
            JobState.PUBLISHING,
            attempt_token=attempt_token,
        )

    def complete_publishing(
        self,
        job_id: str,
        attempt_token: str,
        result_path: str,
        result_bytes: int,
        now: int | None = None,
    ) -> bool:
        """发布共享结果，并在同一事务中完成 owner 与所有 follower。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_id = ? AND job_state = ? AND cache_role = ?
                      AND active_attempt_token = ?
                """,
                (job_id, JobState.PUBLISHING.value, CacheRole.OWNER.value, attempt_token),
            ).fetchone()
            if owner is None:
                connection.commit()
                return False
            cache_updated = connection.execute(
                """
                UPDATE parse_cache
                SET cache_state = ?, result_path = ?, result_bytes = ?,
                    last_accessed_at = ?, expires_at = ?
                WHERE tenant_id = ? AND source_sha256 = ? AND owner_job_id = ?
                      AND cache_state = ?
                """,
                (
                    CacheState.READY.value,
                    result_path,
                    result_bytes,
                    now,
                    now + self.settings.cache_ttl_seconds,
                    owner["tenant_id"],
                    owner["source_sha256"],
                    job_id,
                    CacheState.PROCESSING.value,
                ),
            )
            if cache_updated.rowcount != 1:
                connection.commit()
                return False
            result_expires_at = now + self.settings.result_ttl_seconds
            connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, result_path = ?, result_expires_at = ?, finished_at = ?,
                    active_attempt_token = NULL, lease_expires_at = NULL,
                    error_code = NULL, error_message = NULL
                WHERE job_id = ? AND job_state = ? AND active_attempt_token = ?
                """,
                (
                    JobState.SUCCEEDED.value,
                    result_path,
                    result_expires_at,
                    now,
                    job_id,
                    JobState.PUBLISHING.value,
                    attempt_token,
                ),
            )
            self._enqueue_terminal_callback(connection, job_id, now)
            connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, result_path = ?, result_expires_at = ?, finished_at = ?
                WHERE tenant_id = ? AND source_sha256 = ? AND job_state = ?
                      AND cache_role = ?
                """,
                (
                    JobState.SUCCEEDED.value,
                    result_path,
                    result_expires_at,
                    now,
                    owner["tenant_id"],
                    owner["source_sha256"],
                    JobState.WAITING_FOR_RESULT.value,
                    CacheRole.FOLLOWER.value,
                ),
            )
            follower_rows = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE tenant_id = ? AND source_sha256 = ? AND job_state = ?
                      AND cache_role = ?
                """,
                (
                    owner["tenant_id"],
                    owner["source_sha256"],
                    JobState.SUCCEEDED.value,
                    CacheRole.FOLLOWER.value,
                ),
            ).fetchall()
            for follower in follower_rows:
                self._enqueue_terminal_callback(connection, follower["job_id"], now)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_owner_job(
        self,
        job_id: str,
        attempt_token: str,
        error_code: str,
        error_message: str,
        now: int | None = None,
    ) -> bool:
        """将解析失败的 owner 收敛为终态，并提升一个等待者或清理 processing 缓存。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_id = ? AND job_state = ? AND cache_role = ?
                      AND active_attempt_token = ?
                """,
                (job_id, JobState.RUNNING.value, CacheRole.OWNER.value, attempt_token),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            owner = dict(row)
            connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                    error_code = ?, error_message = ?, active_attempt_token = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ? AND job_state = ? AND active_attempt_token = ?
                """,
                (
                    JobState.FAILED.value,
                    now,
                    now + self.settings.tombstone_ttl_seconds,
                    error_code,
                    error_message,
                    job_id,
                    JobState.RUNNING.value,
                    attempt_token,
                ),
            )
            self._enqueue_terminal_callback(connection, job_id, now)
            self._promote_follower_or_clear_cache(connection, owner, now)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_running_jobs(self, now: int | None = None) -> int:
        """处理失去租约的 running owner，重新排队或在达到上限后失败。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_state = ? AND cache_role = ? AND lease_expires_at <= ?
                ORDER BY queue_seq ASC
                """,
                (JobState.RUNNING.value, CacheRole.OWNER.value, now),
            ).fetchall()
            for row in rows:
                job = dict(row)
                if job["processing_attempt"] >= self.settings.max_processing_attempts:
                    updated = connection.execute(
                        """
                        UPDATE jobs
                        SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                            error_code = ?, error_message = ?, active_attempt_token = NULL,
                            lease_expires_at = NULL
                        WHERE job_id = ? AND job_state = ? AND lease_expires_at <= ?
                        """,
                        (
                            JobState.FAILED.value,
                            now,
                            now + self.settings.tombstone_ttl_seconds,
                            "OCR_LEASE_EXPIRED",
                            "OCR Worker 租约已失效，且已达到最大处理次数。",
                            job["job_id"],
                            JobState.RUNNING.value,
                            now,
                        ),
                    )
                    if updated.rowcount == 1:
                        self._enqueue_terminal_callback(connection, job["job_id"], now)
                        self._promote_follower_or_clear_cache(connection, job, now)
                    continue
                connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, queue_seq = ?, active_attempt_token = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ? AND job_state = ? AND lease_expires_at <= ?
                    """,
                    (
                        JobState.QUEUED.value,
                        self._next_queue_seq(connection),
                        job["job_id"],
                        JobState.RUNNING.value,
                        now,
                    ),
                )
            connection.commit()
            return len(rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_overlong_running_jobs(self, now: int | None = None) -> int:
        """终止超过最大运行时长的 owner，避免持续续租的异常任务阻塞队列。"""

        now = _current_timestamp() if now is None else now
        deadline = now - self.settings.max_run_seconds
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_state = ? AND cache_role = ? AND started_at <= ?
                ORDER BY queue_seq ASC
                """,
                (JobState.RUNNING.value, CacheRole.OWNER.value, deadline),
            ).fetchall()
            for row in rows:
                job = dict(row)
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                        error_code = ?, error_message = ?, active_attempt_token = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ? AND job_state = ? AND started_at <= ?
                    """,
                    (
                        JobState.FAILED.value,
                        now,
                        now + self.settings.tombstone_ttl_seconds,
                        "OCR_RUN_TIMEOUT",
                        "任务运行时间超过 RAPID_DOC_JOB_MAX_RUN_MINUTES。",
                        job["job_id"],
                        JobState.RUNNING.value,
                        deadline,
                    ),
                )
                if updated.rowcount == 1:
                    self._enqueue_terminal_callback(connection, job["job_id"], now)
                    self._promote_follower_or_clear_cache(connection, job, now)
            connection.commit()
            return len(rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_publishing_jobs(self) -> list[dict[str, Any]]:
        """列出需要由维护进程检查落盘结果的发布中 owner。"""

        connection = connect_database(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_state = ? AND cache_role = ? AND active_attempt_token IS NOT NULL
                ORDER BY started_at ASC, job_id ASC
                """,
                (JobState.PUBLISHING.value, CacheRole.OWNER.value),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def requeue_incomplete_publishing(
        self,
        job_id: str,
        attempt_token: str,
        now: int | None = None,
    ) -> bool:
        """发布文件不完整时收敛 attempt：可重试则重排，否则提升 follower。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_id = ? AND job_state = ? AND cache_role = ?
                      AND active_attempt_token = ?
                """,
                (job_id, JobState.PUBLISHING.value, CacheRole.OWNER.value, attempt_token),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            job = dict(row)
            if job["processing_attempt"] >= self.settings.max_processing_attempts:
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                        error_code = ?, error_message = ?, active_attempt_token = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ? AND job_state = ? AND active_attempt_token = ?
                    """,
                    (
                        JobState.FAILED.value,
                        now,
                        now + self.settings.tombstone_ttl_seconds,
                        "PUBLISHING_ARTIFACT_MISSING",
                        "任务在发布阶段中断，且已达到最大处理次数。",
                        job_id,
                        JobState.PUBLISHING.value,
                        attempt_token,
                    ),
                )
                if updated.rowcount == 1:
                    self._enqueue_terminal_callback(connection, job_id, now)
                    self._promote_follower_or_clear_cache(connection, job, now)
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, queue_seq = ?, active_attempt_token = NULL,
                        lease_expires_at = NULL, error_code = ?, error_message = ?
                    WHERE job_id = ? AND job_state = ? AND active_attempt_token = ?
                    """,
                    (
                        JobState.QUEUED.value,
                        self._next_queue_seq(connection),
                        "PUBLISHING_ARTIFACT_MISSING",
                        "任务在发布阶段中断，已重新排队。",
                        job_id,
                        JobState.PUBLISHING.value,
                        attempt_token,
                    ),
                )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def expire_queued_jobs(self, now: int | None = None) -> int:
        """使超出保留期的未完成任务终态化，并解除超期 owner 的缓存互斥。"""

        now = _current_timestamp() if now is None else now
        deadline = now - self.settings.queue_expire_seconds
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            follower_rows = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE job_state = ? AND cache_role = ? AND submitted_at <= ?
                """,
                (JobState.WAITING_FOR_RESULT.value, CacheRole.FOLLOWER.value, deadline),
            ).fetchall()
            for row in follower_rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                        error_code = ?, error_message = ?
                    WHERE job_id = ? AND job_state = ? AND cache_role = ?
                    """,
                    (
                        JobState.EXPIRED.value,
                        now,
                        now + self.settings.tombstone_ttl_seconds,
                        "QUEUE_EXPIRED",
                        "任务等待共享结果时间超过 RAPID_DOC_QUEUE_EXPIRE_MINUTES。",
                        row["job_id"],
                        JobState.WAITING_FOR_RESULT.value,
                        CacheRole.FOLLOWER.value,
                    ),
                )
                self._enqueue_terminal_callback(connection, row["job_id"], now)

            owner_rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE job_state = ? AND cache_role = ? AND submitted_at <= ?
                ORDER BY queue_seq ASC
                """,
                (JobState.QUEUED.value, CacheRole.OWNER.value, deadline),
            ).fetchall()
            for row in owner_rows:
                job = dict(row)
                connection.execute(
                    """
                    UPDATE jobs
                    SET job_state = ?, finished_at = ?, tombstone_expires_at = ?,
                        error_code = ?, error_message = ?
                    WHERE job_id = ? AND job_state = ?
                    """,
                    (
                        JobState.EXPIRED.value,
                        now,
                        now + self.settings.tombstone_ttl_seconds,
                        "QUEUE_EXPIRED",
                        "任务排队时间超过 RAPID_DOC_QUEUE_EXPIRE_MINUTES。",
                        job["job_id"],
                        JobState.QUEUED.value,
                    ),
                )
                self._enqueue_terminal_callback(connection, job["job_id"], now)
                self._promote_follower_or_clear_cache(connection, job, now)
            connection.commit()
            return len(follower_rows) + len(owner_rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def expire_job_results(self, now: int | None = None) -> int:
        """让成功 Job 停止对外提供结果，但不抢先删除仍可能被引用的缓存。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            updated = connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, tombstone_expires_at = ?
                WHERE job_state = ? AND result_expires_at <= ?
                """,
                (
                    JobState.RESULT_EXPIRED.value,
                    now + self.settings.tombstone_ttl_seconds,
                    JobState.SUCCEEDED.value,
                    now,
                ),
            )
            return updated.rowcount
        finally:
            connection.close()

    def remove_expired_caches(self, now: int | None = None) -> list[dict[str, str]]:
        """删除没有活跃成功 Job 引用的过期缓存记录，返回待删文件坐标。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        removed: list[dict[str, str]] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM parse_cache
                WHERE cache_state = ? AND expires_at <= ?
                """,
                (CacheState.READY.value, now),
            ).fetchall()
            for cache in rows:
                active_result = connection.execute(
                    """
                    SELECT MAX(result_expires_at) FROM jobs
                    WHERE tenant_id = ? AND source_sha256 = ? AND job_state = ?
                          AND result_expires_at > ?
                    """,
                    (
                        cache["tenant_id"],
                        cache["source_sha256"],
                        JobState.SUCCEEDED.value,
                        now,
                    ),
                ).fetchone()[0]
                if active_result is not None:
                    connection.execute(
                        """
                        UPDATE parse_cache SET expires_at = ?, last_accessed_at = ?
                        WHERE tenant_id = ? AND source_sha256 = ?
                        """,
                        (active_result, now, cache["tenant_id"], cache["source_sha256"]),
                    )
                    continue
                connection.execute(
                    "DELETE FROM parse_cache WHERE tenant_id = ? AND source_sha256 = ?",
                    (cache["tenant_id"], cache["source_sha256"]),
                )
                removed.append(
                    {"tenant_id": cache["tenant_id"], "source_sha256": cache["source_sha256"]}
                )
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def purge_expired_tombstones(self, now: int | None = None) -> list[str]:
        """删除不再被缓存记录引用的终态 Job，返回可清理输入与 attempt 目录的 ID。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT jobs.job_id FROM jobs
                LEFT JOIN parse_cache ON parse_cache.owner_job_id = jobs.job_id
                WHERE jobs.tombstone_expires_at <= ? AND parse_cache.owner_job_id IS NULL
                """,
                (now,),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            if job_ids:
                connection.executemany("DELETE FROM jobs WHERE job_id = ?", ((job_id,) for job_id in job_ids))
            connection.commit()
            return job_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next_callback(self, now: int | None = None) -> dict[str, Any] | None:
        """领取一条待投递回调，并在发起网络请求前永久标记为 dispatching。"""

        now = _current_timestamp() if now is None else now
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM callback_outbox
                WHERE callback_state = ?
                ORDER BY delivery_id ASC
                LIMIT 1
                """,
                (CallbackState.PENDING.value,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE callback_outbox
                SET callback_state = ?, attempted_at = ?
                WHERE delivery_id = ? AND callback_state = ?
                """,
                (
                    CallbackState.DISPATCHING.value,
                    now,
                    row["delivery_id"],
                    CallbackState.PENDING.value,
                ),
            )
            if updated.rowcount != 1:
                connection.commit()
                return None
            claimed = connection.execute(
                "SELECT * FROM callback_outbox WHERE delivery_id = ?",
                (row["delivery_id"],),
            ).fetchone()
            connection.commit()
            return dict(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_callback(
        self,
        delivery_id: str,
        *,
        delivered: bool,
        http_status: int | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """记录唯一一次回调的最终结果；dispatching 记录不会被重复投递。"""

        state = CallbackState.DELIVERED if delivered else CallbackState.FAILED
        connection = connect_database(self.database_path)
        try:
            updated = connection.execute(
                """
                UPDATE callback_outbox
                SET callback_state = ?, http_status = ?, error_code = ?, error_message = ?
                WHERE delivery_id = ? AND callback_state = ?
                """,
                (
                    state.value,
                    http_status,
                    error_code,
                    error_message,
                    delivery_id,
                    CallbackState.DISPATCHING.value,
                ),
            )
            return updated.rowcount == 1
        finally:
            connection.close()

    def transition_job(
        self,
        job_id: str,
        expected_state: JobState,
        new_state: JobState,
        *,
        attempt_token: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> bool:
        updates = {} if updates is None else dict(updates)
        invalid_columns = set(updates) - _MUTABLE_JOB_COLUMNS
        if invalid_columns:
            raise ValueError(f"unsupported Job update columns: {sorted(invalid_columns)}")

        assignments = ["job_state = ?"]
        parameters: list[Any] = [new_state.value]
        for column, value in updates.items():
            assignments.append(f"{column} = ?")
            parameters.append(value)
        where_parts = ["job_id = ?", "job_state = ?"]
        parameters.extend([job_id, expected_state.value])
        if attempt_token is not None:
            where_parts.append("active_attempt_token = ?")
            parameters.append(attempt_token)

        connection = connect_database(self.database_path)
        try:
            updated = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE {' AND '.join(where_parts)}",
                parameters,
            )
            return updated.rowcount == 1
        finally:
            connection.close()

    def _ensure_queue_capacity(self, connection: sqlite3.Connection) -> None:
        queued_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_state = ?", (JobState.QUEUED.value,)
        ).fetchone()[0]
        if queued_count >= JobAdmissionLimits.MAX_QUEUED_JOBS:
            raise QueueCapacityError("the OCR queue is at capacity")

    def _promote_follower_or_clear_cache(
        self, connection: sqlite3.Connection, owner: dict[str, Any], now: int
    ) -> None:
        follower = connection.execute(
            """
            SELECT * FROM jobs
            WHERE tenant_id = ? AND source_sha256 = ? AND job_state = ? AND cache_role = ?
            ORDER BY submitted_at ASC, job_id ASC
            LIMIT 1
            """,
            (
                owner["tenant_id"],
                owner["source_sha256"],
                JobState.WAITING_FOR_RESULT.value,
                CacheRole.FOLLOWER.value,
            ),
        ).fetchone()
        if follower is None:
            connection.execute(
                """
                DELETE FROM parse_cache
                WHERE tenant_id = ? AND source_sha256 = ? AND owner_job_id = ?
                      AND cache_state = ?
                """,
                (
                    owner["tenant_id"],
                    owner["source_sha256"],
                    owner["job_id"],
                    CacheState.PROCESSING.value,
                ),
            )
            return

        queue_seq = self._next_queue_seq(connection)
        connection.execute(
            """
            UPDATE jobs SET job_state = ?, cache_role = ?, queue_seq = ?
            WHERE job_id = ? AND job_state = ? AND cache_role = ?
            """,
            (
                JobState.QUEUED.value,
                CacheRole.OWNER.value,
                queue_seq,
                follower["job_id"],
                JobState.WAITING_FOR_RESULT.value,
                CacheRole.FOLLOWER.value,
            ),
        )
        connection.execute(
            """
            UPDATE parse_cache SET owner_job_id = ?, last_accessed_at = ?
            WHERE tenant_id = ? AND source_sha256 = ? AND owner_job_id = ?
                  AND cache_state = ?
            """,
            (
                follower["job_id"],
                now,
                owner["tenant_id"],
                owner["source_sha256"],
                owner["job_id"],
                CacheState.PROCESSING.value,
            ),
        )

    @staticmethod
    def _enqueue_terminal_callback(
        connection: sqlite3.Connection, job_id: str, now: int
    ) -> None:
        """在 Job 终态所在的同一事务内创建唯一 outbox 记录。"""

        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if (
            job is None
            or not job["callback_url"]
            or job["job_state"] not in _CALLBACK_TERMINAL_STATES
        ):
            return
        delivery_id = generate_ulid(timestamp_ms=now * 1000)
        payload = {
            "deliveryId": delivery_id,
            "eventType": "job.terminal",
            "jobId": job["job_id"],
            "jobState": job["job_state"],
            "submittedAt": _timestamp_as_iso(job["submitted_at"]),
            "startedAt": _timestamp_as_iso(job["started_at"]),
            "finishedAt": _timestamp_as_iso(job["finished_at"]),
            "resultUrl": f"/jobs/{job['job_id']}/result",
            "error": (
                {"code": job["error_code"], "message": job["error_message"] or ""}
                if job["error_code"]
                else None
            ),
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO callback_outbox (
                delivery_id, job_id, callback_url_snapshot, payload_json, callback_state
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                job_id,
                job["callback_url"],
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                CallbackState.PENDING.value,
            ),
        )

    @staticmethod
    def _select_job(
        connection: sqlite3.Connection, tenant_id: str, job_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT jobs.*, callback_outbox.callback_state
            FROM jobs
            LEFT JOIN callback_outbox ON callback_outbox.job_id = jobs.job_id
            WHERE jobs.tenant_id = ? AND jobs.job_id = ?
            """,
            (tenant_id, job_id),
        ).fetchone()

    @staticmethod
    def _next_queue_seq(connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE job_queue_sequence SET last_value = last_value + 1 WHERE sequence_name = 'ocr'"
        )
        return connection.execute(
            "SELECT last_value FROM job_queue_sequence WHERE sequence_name = 'ocr'"
        ).fetchone()[0]

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        submission: JobSubmission,
        idempotency_key_hash: str | None,
        now: int,
        warnings_json: str | None,
        job_state: JobState,
        cache_role: CacheRole,
        queue_seq: int | None = None,
        result_path: str | None = None,
        result_expires_at: int | None = None,
        finished_at: int | None = None,
    ) -> dict[str, Any]:
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, tenant_id, queue_seq, idempotency_key_hash, request_fingerprint,
                source_filename, stored_filename, business_ref, source_sha256,
                source_bytes, job_state, cache_role, callback_url, result_path,
                source_page_count, processed_page_count, truncated, warnings_json,
                result_expires_at, submitted_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                submission.tenant_id,
                queue_seq,
                idempotency_key_hash,
                submission.request_fingerprint,
                submission.source_filename,
                submission.stored_filename,
                submission.business_ref,
                submission.source_sha256,
                submission.source_bytes,
                job_state.value,
                cache_role.value,
                submission.callback_url,
                result_path,
                submission.source_page_count,
                submission.processed_page_count,
                int(submission.truncated),
                warnings_json,
                result_expires_at,
                now,
                finished_at,
            ),
        )
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row)


def hash_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _current_timestamp() -> int:
    return int(time.time())


def _timestamp_as_iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
