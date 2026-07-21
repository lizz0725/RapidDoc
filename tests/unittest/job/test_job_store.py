"""异步 JobStore 的事务行为测试。"""

from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database, initialize_database
from rapid_doc.jobs.job_store import (
    IdempotencyConflictError,
    JobStore,
    JobSubmission,
)
from rapid_doc.jobs.job_types import CacheRole, CacheState, JobState


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = JobSettings(data_dir=self.root)
        initialize_database(self.settings.database_path)
        self.store = JobStore(self.settings.database_path, self.settings)
        self.now = 1_700_000_000

    def submission(
        self,
        *,
        digest: str,
        fingerprint: str = "request-v1",
        idempotency_key: str | None = None,
    ) -> JobSubmission:
        return JobSubmission(
            tenant_id="finance",
            request_fingerprint=fingerprint,
            source_filename="contract.pdf",
            stored_filename="contract.pdf",
            source_sha256=digest,
            source_bytes=12,
            idempotency_key=idempotency_key,
        )

    def test_new_file_creates_owner_and_same_file_creates_follower(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission(digest="a" * 64), now=self.now
        )
        follower = self.store.create_or_reuse_job(
            self.submission(digest="a" * 64, fingerprint="request-v2"), now=self.now + 1
        )

        self.assertEqual(owner.job["job_state"], JobState.QUEUED.value)
        self.assertEqual(owner.job["cache_role"], CacheRole.OWNER.value)
        self.assertEqual(owner.job["queue_seq"], 1)
        self.assertEqual(follower.job["job_state"], JobState.WAITING_FOR_RESULT.value)
        self.assertEqual(follower.job["cache_role"], CacheRole.FOLLOWER.value)
        self.assertIsNone(follower.job["queue_seq"])

    def test_ready_cache_returns_succeeded_hit(self) -> None:
        owner = self.store.create_or_reuse_job(
            self.submission(digest="b" * 64), now=self.now
        )
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
                    "cache/finance/result.json",
                    42,
                    "finance",
                    "b" * 64,
                    owner.job["job_id"],
                ),
            )
        finally:
            connection.close()

        hit = self.store.create_or_reuse_job(
            self.submission(digest="b" * 64, fingerprint="request-v2"), now=self.now + 1
        )

        self.assertEqual(hit.job["job_state"], JobState.SUCCEEDED.value)
        self.assertEqual(hit.job["cache_role"], CacheRole.HIT.value)
        self.assertEqual(hit.job["result_path"], "cache/finance/result.json")

    def test_idempotency_reuses_same_request_and_rejects_different_request(self) -> None:
        first = self.store.create_or_reuse_job(
            self.submission(digest="c" * 64, idempotency_key="retry-1"), now=self.now
        )
        retry = self.store.create_or_reuse_job(
            self.submission(digest="c" * 64, idempotency_key="retry-1"), now=self.now + 1
        )

        self.assertTrue(retry.reused_idempotency_key)
        self.assertEqual(retry.job["job_id"], first.job["job_id"])
        with self.assertRaises(IdempotencyConflictError):
            self.store.create_or_reuse_job(
                self.submission(
                    digest="d" * 64,
                    fingerprint="request-v2",
                    idempotency_key="retry-1",
                ),
                now=self.now + 2,
            )

    def test_concurrent_workers_claim_distinct_jobs_in_fifo_order(self) -> None:
        first = self.store.create_or_reuse_job(
            self.submission(digest="e" * 64), now=self.now
        )
        second = self.store.create_or_reuse_job(
            self.submission(digest="f" * 64), now=self.now + 1
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    lambda index: self.store.claim_next_job(
                        attempt_token=f"attempt-{index}",
                        now=self.now + 10 + index,
                    ),
                    range(2),
                )
            )

        self.assertEqual({claim["job_id"] for claim in claims}, {first.job["job_id"], second.job["job_id"]})
        self.assertEqual(
            sorted(claim["queue_seq"] for claim in claims),
            [first.job["queue_seq"], second.job["queue_seq"]],
        )
        self.assertTrue(
            self.store.renew_lease(claims[0]["job_id"], claims[0]["active_attempt_token"], self.now + 20)
        )
        self.assertFalse(
            self.store.transition_job(
                claims[0]["job_id"],
                JobState.RUNNING,
                JobState.PUBLISHING,
                attempt_token="stale-attempt",
            )
        )


if __name__ == "__main__":
    unittest.main()
