"""Watchdog、Sweeper 与文件回收的无模型单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_artifacts import ArtifactStore
from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import initialize_database
from rapid_doc.jobs.job_maintenance import JobMaintenance
from rapid_doc.jobs.job_store import JobStore, JobSubmission
from rapid_doc.jobs.job_types import CacheRole, JobState


class JobMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = JobSettings(
            data_dir=self.root / "jobs",
            queue_expire_minutes=1,
            result_ttl_minutes=1,
            cache_ttl_minutes=1,
            max_run_minutes=1,
            tombstone_ttl_minutes=1,
            max_processing_attempts=2,
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        initialize_database(self.settings.database_path)
        self.artifacts = ArtifactStore(self.settings.data_dir)
        self.artifacts.ensure_layout()
        self.store = JobStore(self.settings.database_path, self.settings)
        self.maintenance = JobMaintenance(
            self.settings, store=self.store, artifacts=self.artifacts
        )
        self.now = 1_700_000_000

    def enqueue(self, digest: str, *, fingerprint: str = "request-v1") -> dict[str, object]:
        job = self.store.create_or_reuse_job(
            JobSubmission(
                tenant_id="finance",
                request_fingerprint=fingerprint,
                source_filename="contract.pdf",
                stored_filename="contract.pdf",
                source_sha256=digest,
                source_bytes=len(b"source"),
            ),
            now=self.now,
        ).job
        self.artifacts.write_bytes_atomic(
            self.artifacts.input_path(job["job_id"], job["stored_filename"]), b"source"
        )
        return job

    def test_expired_lease_returns_owner_to_the_fifo_queue(self) -> None:
        owner = self.enqueue("a" * 64)
        claimed = self.store.claim_next_job("attempt-1", now=self.now)
        self.assertEqual(claimed["job_id"], owner["job_id"])

        report = self.maintenance.run_watchdog_once(now=self.now + self.settings.lease_seconds)

        recovered = self.store.get_job("finance", owner["job_id"])
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(report.expired_leases, 1)
        self.assertEqual(recovered["job_state"], JobState.QUEUED.value)
        self.assertEqual(recovered["processing_attempt"], 1)
        self.assertIsNone(recovered["active_attempt_token"])
        self.assertGreater(recovered["queue_seq"], owner["queue_seq"])

    def test_overlong_owner_fails_and_promotes_the_earliest_follower(self) -> None:
        owner = self.enqueue("b" * 64)
        follower = self.enqueue("b" * 64, fingerprint="request-v2")
        self.store.claim_next_job("attempt-1", now=self.now)

        report = self.maintenance.run_watchdog_once(
            now=self.now + self.settings.max_run_seconds
        )

        failed = self.store.get_job("finance", owner["job_id"])
        promoted = self.store.get_job("finance", follower["job_id"])
        self.assertIsNotNone(failed)
        self.assertIsNotNone(promoted)
        assert failed is not None
        assert promoted is not None
        self.assertEqual(report.run_timeouts, 1)
        self.assertEqual(failed["job_state"], JobState.FAILED.value)
        self.assertEqual(failed["error_code"], "OCR_RUN_TIMEOUT")
        self.assertEqual(promoted["job_state"], JobState.QUEUED.value)
        self.assertEqual(promoted["cache_role"], CacheRole.OWNER.value)
        self.assertEqual(
            (self.settings.data_dir / "control" / "restart-workers.request").read_text(
                encoding="utf-8"
            ),
            "检测到 OCR 最大执行时长超限。",
        )

    def test_publishing_attempt_recovers_from_the_temporary_result_file(self) -> None:
        owner = self.enqueue("c" * 64)
        claimed = self.store.claim_next_job("attempt-1", now=self.now)
        self.assertEqual(claimed["job_id"], owner["job_id"])
        self.assertTrue(self.store.begin_publishing(owner["job_id"], "attempt-1"))
        temporary_path = self.artifacts.attempt_result_path(owner["job_id"], "attempt-1")
        self.artifacts.write_bytes_atomic(
            temporary_path,
            json.dumps({"markdown": "# 已恢复", "metadata": {}}).encode("utf-8"),
        )

        report = self.maintenance.run_watchdog_once(now=self.now + 1)

        completed = self.store.get_job("finance", owner["job_id"])
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(report.publishing_recovered, 1)
        self.assertEqual(completed["job_state"], JobState.SUCCEEDED.value)
        result = self.artifacts.read_result_json(completed["result_path"])
        self.assertEqual(result["markdown"], "# 已恢复")
        markdown = self.artifacts.cache_result_path("finance", "c" * 64, "md")
        self.assertEqual(markdown.read_text(encoding="utf-8"), "# 已恢复")

    def test_incomplete_publishing_attempt_requeues_then_fails_at_the_attempt_limit(self) -> None:
        owner = self.enqueue("e" * 64)
        follower = self.enqueue("e" * 64, fingerprint="request-v2")
        self.store.claim_next_job("attempt-1", now=self.now)
        self.assertTrue(self.store.begin_publishing(owner["job_id"], "attempt-1"))

        first_report = self.maintenance.run_watchdog_once(now=self.now + 1)
        requeued = self.store.get_job("finance", owner["job_id"])
        self.assertIsNotNone(requeued)
        assert requeued is not None
        self.assertEqual(first_report.publishing_recovered, 1)
        self.assertEqual(requeued["job_state"], JobState.QUEUED.value)
        self.assertEqual(requeued["processing_attempt"], 1)

        self.store.claim_next_job("attempt-2", now=self.now + 2)
        self.assertTrue(self.store.begin_publishing(owner["job_id"], "attempt-2"))
        second_report = self.maintenance.run_watchdog_once(now=self.now + 3)

        failed = self.store.get_job("finance", owner["job_id"])
        promoted = self.store.get_job("finance", follower["job_id"])
        self.assertIsNotNone(failed)
        self.assertIsNotNone(promoted)
        assert failed is not None
        assert promoted is not None
        self.assertEqual(second_report.publishing_recovered, 1)
        self.assertEqual(failed["job_state"], JobState.FAILED.value)
        self.assertEqual(failed["error_code"], "PUBLISHING_ARTIFACT_MISSING")
        self.assertEqual(promoted["job_state"], JobState.QUEUED.value)

    def test_expired_queue_owner_becomes_expired_and_releases_processing_cache(self) -> None:
        owner = self.enqueue("f" * 64)
        follower = self.enqueue("f" * 64, fingerprint="request-v2")

        report = self.maintenance.run_sweeper_once(
            now=self.now + self.settings.queue_expire_seconds
        )

        expired = self.store.get_job("finance", owner["job_id"])
        expired_follower = self.store.get_job("finance", follower["job_id"])
        self.assertIsNotNone(expired)
        self.assertIsNotNone(expired_follower)
        assert expired is not None
        assert expired_follower is not None
        self.assertEqual(report.queue_expired, 2)
        self.assertEqual(expired["job_state"], JobState.EXPIRED.value)
        self.assertEqual(expired["error_code"], "QUEUE_EXPIRED")
        self.assertEqual(expired_follower["job_state"], JobState.EXPIRED.value)
        self.assertEqual(
            self.store.create_or_reuse_job(
                JobSubmission(
                    tenant_id="finance",
                    request_fingerprint="request-after-expiry",
                    source_filename="contract.pdf",
                    stored_filename="contract.pdf",
                    source_sha256="f" * 64,
                    source_bytes=6,
                ),
                now=self.now + self.settings.queue_expire_seconds + 1,
            ).job["job_state"],
            JobState.QUEUED.value,
        )

    def test_result_cache_and_input_are_cleaned_after_their_retention_periods(self) -> None:
        owner = self.enqueue("d" * 64)
        self.store.claim_next_job("attempt-1", now=self.now)
        self.assertTrue(self.store.begin_publishing(owner["job_id"], "attempt-1"))
        result_path = self.artifacts.cache_result_path("finance", "d" * 64, "json")
        self.artifacts.write_bytes_atomic(
            result_path,
            json.dumps({"markdown": "# 结果", "metadata": {}}).encode("utf-8"),
        )
        self.assertTrue(
            self.store.complete_publishing(
                owner["job_id"],
                "attempt-1",
                str(result_path.relative_to(self.settings.data_dir)),
                result_path.stat().st_size,
                now=self.now,
            )
        )

        first_sweep = self.maintenance.run_sweeper_once(
            now=self.now + self.settings.result_ttl_seconds
        )
        expired = self.store.get_job("finance", owner["job_id"])
        self.assertIsNotNone(expired)
        assert expired is not None
        self.assertEqual(first_sweep.result_expired, 1)
        self.assertEqual(first_sweep.caches_removed, 1)
        self.assertEqual(expired["job_state"], JobState.RESULT_EXPIRED.value)
        self.assertFalse(result_path.exists())

        second_sweep = self.maintenance.run_sweeper_once(
            now=self.now
            + self.settings.result_ttl_seconds
            + self.settings.tombstone_ttl_seconds
        )
        self.assertEqual(second_sweep.jobs_purged, 1)
        self.assertIsNone(self.store.get_job("finance", owner["job_id"]))
        input_path = self.artifacts.input_path(owner["job_id"], owner["stored_filename"])
        self.assertFalse(input_path.exists())


if __name__ == "__main__":
    unittest.main()
