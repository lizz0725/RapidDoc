"""不加载 OCR 模型的异步 Job 基础设施单元测试。"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPOSITORY_ROOT / "docker"
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

from job_artifacts import ArtifactStore, tenant_storage_key  # noqa: E402
from job_config import JobSettings  # noqa: E402
from job_database import connect_database, initialize_database  # noqa: E402
from job_limits import JobAdmissionLimits  # noqa: E402
from job_types import generate_ulid  # noqa: E402


class JobFoundationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_settings_apply_documented_defaults_and_convert_minutes(self) -> None:
        settings = JobSettings.from_env({})

        self.assertEqual(settings.worker_processes, 1)
        self.assertEqual(settings.max_file_size_bytes, 100 * 1024 * 1024)
        self.assertEqual(settings.queue_expire_seconds, 43_200 * 60)
        self.assertIn("xlsx", settings.allowed_extensions)
        self.assertEqual(JobAdmissionLimits.MAX_QUEUED_JOBS, 100)

    def test_settings_reject_incompatible_timing_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "CACHE_TTL"):
            JobSettings.from_env(
                {
                    "RAPID_DOC_RESULT_TTL_MINUTES": "60",
                    "RAPID_DOC_CACHE_TTL_MINUTES": "30",
                }
            )

        with self.assertRaisesRegex(ValueError, "HEARTBEAT"):
            JobSettings.from_env(
                {
                    "RAPID_DOC_JOB_LEASE_SECONDS": "30",
                    "RAPID_DOC_JOB_HEARTBEAT_SECONDS": "30",
                }
            )

    def test_ulids_are_url_safe_and_time_sortable(self) -> None:
        earlier = generate_ulid(timestamp_ms=1_700_000_000_000)
        later = generate_ulid(timestamp_ms=1_700_000_000_001)

        self.assertEqual(len(earlier), 26)
        self.assertRegex(earlier, r"^[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertLess(earlier, later)
        self.assertNotEqual(earlier, generate_ulid(timestamp_ms=1_700_000_000_000))

    def test_database_uses_wal_and_creates_required_tables_and_indexes(self) -> None:
        database_path = self.root / "jobs" / "rapid-doc.db"
        initialize_database(database_path)

        connection = connect_database(database_path)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            connection.close()

        self.assertEqual(journal_mode, "wal")
        self.assertTrue({"jobs", "parse_cache", "callback_outbox", "service_heartbeats"} <= tables)
        self.assertIn("jobs_tenant_idempotency_key_unique", indexes)
        self.assertIn("parse_cache_state_expires_at_index", indexes)

    def test_artifact_store_writes_and_publishes_only_inside_the_root(self) -> None:
        store = ArtifactStore(self.root / "jobs")
        store.ensure_layout()
        job_id = generate_ulid(timestamp_ms=1_700_000_000_000)
        digest = "a" * 64
        input_path = store.input_path(job_id, "source.pdf")
        temporary_path = store.attempt_result_path(job_id, "attempt-token")
        result_path = store.cache_result_path("finance", digest, "md")

        store.write_bytes_atomic(input_path, b"source")
        store.write_bytes_atomic(temporary_path, b"# result")
        store.publish(temporary_path, result_path)

        self.assertEqual(input_path.read_bytes(), b"source")
        self.assertEqual(result_path.read_bytes(), b"# result")
        self.assertFalse(temporary_path.exists())
        self.assertEqual(len(tenant_storage_key("finance")), 64)
        with self.assertRaises(ValueError):
            store.input_path(job_id, "../escape.pdf")

        with self.assertRaises(ValueError):
            store.remove_tree(Path("/tmp"))


if __name__ == "__main__":
    unittest.main()
