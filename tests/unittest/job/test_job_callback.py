"""一次性回调 outbox 与 Dispatcher 的无网络单元测试。"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_callback import CallbackDispatcher
from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database, initialize_database
from rapid_doc.jobs.job_store import JobStore, JobSubmission
from rapid_doc.jobs.job_types import CacheRole, CacheState, CallbackState, JobState


class FixtureSender:
    def __init__(self, status_code: int = 204, error: Exception | None = None) -> None:
        self.status_code = status_code
        self.error = error
        self.calls: list[dict[str, object]] = []

    def send(
        self,
        callback_url: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: tuple[int, int],
    ) -> int:
        self.calls.append(
            {
                "callback_url": callback_url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return self.status_code


class JobCallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = JobSettings(
            data_dir=self.root / "jobs",
            callback_signing_secret="fixture-secret",
        )
        initialize_database(self.settings.database_path)
        self.store = JobStore(self.settings.database_path, self.settings)
        self.now = 1_700_000_000

    def submission(
        self,
        digest: str,
        *,
        callback_url: str | None = None,
        fingerprint: str = "request-v1",
    ) -> JobSubmission:
        return JobSubmission(
            tenant_id="finance",
            request_fingerprint=fingerprint,
            source_filename="contract.pdf",
            stored_filename="contract.pdf",
            source_sha256=digest,
            source_bytes=12,
            callback_url=callback_url,
        )

    def outbox_rows(self) -> list[dict[str, object]]:
        connection = connect_database(self.settings.database_path)
        try:
            rows = connection.execute(
                "SELECT * FROM callback_outbox ORDER BY job_id ASC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def test_success_completion_creates_outboxes_for_owner_and_follower_then_delivers_once(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission("a" * 64, callback_url="https://owner.internal/callback"),
            now=self.now,
        ).job
        follower = self.store.create_or_reuse_job(
            self.submission(
                "a" * 64,
                callback_url="https://follower.internal/callback",
                fingerprint="request-v2",
            ),
            now=self.now + 1,
        ).job
        self.store.claim_next_job("attempt-1", now=self.now + 2)
        self.assertTrue(self.store.begin_publishing(owner["job_id"], "attempt-1"))
        self.assertTrue(
            self.store.complete_publishing(
                owner["job_id"],
                "attempt-1",
                "cache/fixture/result.json",
                42,
                now=self.now + 3,
            )
        )

        rows = self.outbox_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["job_id"] for row in rows}, {owner["job_id"], follower["job_id"]})
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            self.assertEqual(row["callback_state"], CallbackState.PENDING.value)
            self.assertEqual(payload["eventType"], "job.terminal")
            self.assertEqual(payload["jobState"], JobState.SUCCEEDED.value)
            self.assertNotIn("markdown", payload)

        sender = FixtureSender()
        dispatcher = CallbackDispatcher(self.settings, store=self.store, sender=sender)
        self.assertTrue(dispatcher.run_once())
        self.assertTrue(dispatcher.run_once())
        self.assertFalse(dispatcher.run_once())
        self.assertEqual(len(sender.calls), 2)
        first_call = sender.calls[0]
        expected_signature = hmac.new(
            b"fixture-secret", first_call["payload"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(
            first_call["headers"]["X-RapidDoc-Signature"], f"v1={expected_signature}"
        )
        self.assertEqual(first_call["timeout"], (5, 30))
        self.assertEqual(
            {row["callback_state"] for row in self.outbox_rows()},
            {CallbackState.DELIVERED.value},
        )
        self.assertEqual(
            self.store.get_job("finance", owner["job_id"])["job_state"],
            JobState.SUCCEEDED.value,
        )

    def test_failed_http_callback_does_not_change_failed_job_or_retry(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission("b" * 64, callback_url="https://biz.internal/callback"), now=self.now
        ).job
        self.store.claim_next_job("attempt-1", now=self.now + 1)
        self.assertTrue(
            self.store.fail_owner_job(
                owner["job_id"],
                "attempt-1",
                "OCR_PARSE_FAILED",
                "fixture parse failure",
                now=self.now + 2,
            )
        )

        sender = FixtureSender(status_code=503)
        dispatcher = CallbackDispatcher(self.settings, store=self.store, sender=sender)
        self.assertTrue(dispatcher.run_once())
        self.assertFalse(dispatcher.run_once())

        row = self.outbox_rows()[0]
        self.assertEqual(row["callback_state"], CallbackState.FAILED.value)
        self.assertEqual(row["http_status"], 503)
        self.assertEqual(row["error_code"], "CALLBACK_HTTP_STATUS")
        job = self.store.get_job("finance", owner["job_id"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["job_state"], JobState.FAILED.value)
        self.assertEqual(job["callback_state"], CallbackState.FAILED.value)
        self.assertEqual(len(sender.calls), 1)

    def test_network_error_marks_callback_failed_without_a_second_attempt(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission("bc" * 32, callback_url="https://biz.internal/callback"),
            now=self.now,
        ).job
        self.assertTrue(self.store.cancel_job("finance", owner["job_id"], now=self.now + 1).cancelled)

        sender = FixtureSender(error=OSError("network unavailable"))
        dispatcher = CallbackDispatcher(self.settings, store=self.store, sender=sender)
        self.assertTrue(dispatcher.run_once())
        self.assertFalse(dispatcher.run_once())

        row = self.outbox_rows()[0]
        self.assertEqual(row["callback_state"], CallbackState.FAILED.value)
        self.assertIsNone(row["http_status"])
        self.assertEqual(row["error_code"], "CALLBACK_REQUEST_ERROR")
        self.assertEqual(len(sender.calls), 1)

    def test_cancelled_job_creates_only_one_outbox_even_when_cancel_is_repeated(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission("c" * 64, callback_url="https://biz.internal/callback"), now=self.now
        ).job

        first = self.store.cancel_job("finance", owner["job_id"], now=self.now + 1)
        second = self.store.cancel_job("finance", owner["job_id"], now=self.now + 2)

        self.assertTrue(first.cancelled)
        self.assertFalse(second.cancelled)
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        payload = json.loads(str(rows[0]["payload_json"]))
        self.assertEqual(payload["jobState"], JobState.CANCELLED.value)

    def test_cache_hit_creates_pending_callback_without_reprocessing(self) -> None:
        owner = self.store.create_or_reuse_job(self.submission("d" * 64), now=self.now).job
        connection = connect_database(self.settings.database_path)
        try:
            connection.execute(
                """
                UPDATE parse_cache
                SET cache_state = ?, result_path = ?, result_bytes = ?
                WHERE tenant_id = ? AND source_sha256 = ? AND owner_job_id = ?
                """,
                (
                    CacheState.READY.value,
                    "cache/fixture/result.json",
                    42,
                    "finance",
                    "d" * 64,
                    owner["job_id"],
                ),
            )
        finally:
            connection.close()

        hit = self.store.create_or_reuse_job(
            self.submission(
                "d" * 64,
                callback_url="https://biz.internal/callback",
                fingerprint="request-v2",
            ),
            now=self.now + 1,
        ).job

        self.assertEqual(hit["job_state"], JobState.SUCCEEDED.value)
        self.assertEqual(hit["cache_role"], CacheRole.HIT.value)
        row = self.outbox_rows()[0]
        self.assertEqual(row["job_id"], hit["job_id"])
        self.assertEqual(row["callback_state"], CallbackState.PENDING.value)


if __name__ == "__main__":
    unittest.main()
