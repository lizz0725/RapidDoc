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
from pathlib import Path
from typing import Any

from .job_config import JobSettings
from .job_database import connect_database
from .job_limits import JobAdmissionLimits
from .job_types import CacheRole, CacheState, JobState, generate_ulid


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
                    UPDATE parse_cache SET last_accessed_at = ?
                    WHERE tenant_id = ? AND source_sha256 = ?
                    """,
                    (now, submission.tenant_id, submission.source_sha256),
                )
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
                UPDATE jobs SET job_state = ?, finished_at = ?
                WHERE tenant_id = ? AND job_id = ? AND job_state = ?
                """,
                (JobState.CANCELLED.value, now, tenant_id, job_id, job["job_state"]),
            )
            if updated.rowcount != 1:
                connection.commit()
                return JobCancellation(job=job, cancelled=False)

            if job["job_state"] == JobState.QUEUED.value and job["cache_role"] == CacheRole.OWNER.value:
                self._promote_follower_or_clear_cache(connection, job, now)

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
                    started_at = COALESCE(started_at, ?)
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
