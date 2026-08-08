"""OCR Worker 的无模型状态与结果发布测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_artifacts import ArtifactStore
from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database, initialize_database
from rapid_doc.jobs.job_parser import JobParseError, ParsedDocument
from rapid_doc.jobs.job_store import JobStore, JobSubmission
from rapid_doc.jobs.job_types import CacheRole, CacheState, JobState
from rapid_doc.jobs.job_worker import JobWorker
from tests.unittest.job.test_support import mysql_test_settings


class FixtureParser:
    def __init__(self, markdown: str = "# fixture") -> None:
        self.markdown = markdown
        self.calls: list[str] = []

    def parse(self, job: dict[str, object], input_path: Path, output_dir: Path) -> ParsedDocument:
        self.calls.append(str(job["job_id"]))
        self.assert_input(input_path)
        return ParsedDocument(self.markdown, {"engine": "fixture"})

    @staticmethod
    def assert_input(input_path: Path) -> None:
        if input_path.read_bytes() != b"source":
            raise AssertionError("Worker 未读取预期的任务原始文件")


class FailingParser:
    def parse(self, job: dict[str, object], input_path: Path, output_dir: Path) -> ParsedDocument:
        raise JobParseError("fixture parser failed")


class JobWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = mysql_test_settings(self.root / "jobs")
        self.artifacts = ArtifactStore(self.settings.data_dir)
        self.artifacts.ensure_layout()
        self.store = JobStore(self.settings, self.settings)
        self.now = 1_700_000_000

    def enqueue(self, digest: str, *, fingerprint: str = "request-v1") -> dict[str, object]:
        creation = self.store.create_or_reuse_job(
            JobSubmission(
                tenant_id="finance",
                request_fingerprint=fingerprint,
                source_filename="contract.pdf",
                stored_filename="contract.pdf",
                source_sha256=digest,
                source_bytes=len(b"source"),
            ),
            now=self.now,
        )
        job = creation.job
        self.artifacts.write_bytes_atomic(
            self.artifacts.input_path(job["job_id"], job["stored_filename"]), b"source"
        )
        return job

    def test_successful_owner_publishes_shared_result_and_completes_followers(self) -> None:
        owner = self.enqueue("a" * 64)
        follower = self.enqueue("a" * 64, fingerprint="request-v2")
        parser = FixtureParser("# 合同\n\n正文")
        worker = JobWorker(self.settings, store=self.store, artifacts=self.artifacts, parser=parser)

        self.assertTrue(worker.run_once())

        completed_owner = self.store.get_job("finance", owner["job_id"])
        completed_follower = self.store.get_job("finance", follower["job_id"])
        self.assertIsNotNone(completed_owner)
        self.assertIsNotNone(completed_follower)
        assert completed_owner is not None
        assert completed_follower is not None
        self.assertEqual(completed_owner["job_state"], JobState.SUCCEEDED.value)
        self.assertEqual(completed_follower["job_state"], JobState.SUCCEEDED.value)
        self.assertEqual(completed_owner["cache_role"], CacheRole.OWNER.value)
        self.assertEqual(completed_follower["cache_role"], CacheRole.FOLLOWER.value)
        self.assertEqual(completed_owner["result_path"], completed_follower["result_path"])
        result = self.artifacts.read_result_json(completed_owner["result_path"])
        self.assertEqual(result["markdown"], "# 合同\n\n正文")
        self.assertEqual(result["metadata"]["engine"], "fixture")
        connection = connect_database(self.settings)
        try:
            cache = connection.execute(
                "SELECT cache_state, result_bytes FROM parse_cache WHERE tenant_id = ? AND source_sha256 = ?",
                ("finance", "a" * 64),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(cache["cache_state"], CacheState.READY.value)
        self.assertGreater(cache["result_bytes"], 0)
        self.assertEqual(parser.calls, [owner["job_id"]])
        self.assertFalse(worker.run_once())

    def test_failed_owner_promotes_follower_for_the_next_attempt(self) -> None:
        owner = self.enqueue("b" * 64)
        follower = self.enqueue("b" * 64, fingerprint="request-v2")
        failing_worker = JobWorker(
            self.settings, store=self.store, artifacts=self.artifacts, parser=FailingParser()
        )

        self.assertTrue(failing_worker.run_once())

        failed_owner = self.store.get_job("finance", owner["job_id"])
        promoted_follower = self.store.get_job("finance", follower["job_id"])
        self.assertIsNotNone(failed_owner)
        self.assertIsNotNone(promoted_follower)
        assert failed_owner is not None
        assert promoted_follower is not None
        self.assertEqual(failed_owner["job_state"], JobState.FAILED.value)
        self.assertEqual(failed_owner["error_code"], "OCR_PARSE_FAILED")
        self.assertEqual(promoted_follower["job_state"], JobState.QUEUED.value)
        self.assertEqual(promoted_follower["cache_role"], CacheRole.OWNER.value)

        successful_worker = JobWorker(
            self.settings, store=self.store, artifacts=self.artifacts, parser=FixtureParser()
        )
        self.assertTrue(successful_worker.run_once())
        self.assertEqual(
            self.store.get_job("finance", follower["job_id"])["job_state"],
            JobState.SUCCEEDED.value,
        )


if __name__ == "__main__":
    unittest.main()
