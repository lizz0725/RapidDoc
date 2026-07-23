"""不加载 OCR 模型的异步 Job 基础设施单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_artifacts import ArtifactStore, tenant_storage_key
from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database, initialize_database
from rapid_doc.jobs.job_limits import JobAdmissionLimits
from rapid_doc.jobs.job_types import generate_ulid
from tests.unittest.job.test_support import mysql_test_settings


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
        self.assertEqual(settings.background_heartbeat_fresh_seconds, 90)
        self.assertIn("xlsx", settings.allowed_extensions)
        self.assertEqual(JobAdmissionLimits.MAX_QUEUED_JOBS, 100)

        custom_directory = JobSettings.from_env(
            {"RAPID_DOC_JOB_DATA_DIR": "/data/rapid-doc/jobs"}
        )
        self.assertEqual(custom_directory.data_dir, Path("/data/rapid-doc/jobs"))

    def test_mysql_settings_are_parsed(self) -> None:
        settings = JobSettings.from_env(
            {
                "RAPID_DOC_DB_BACKEND": "mysql",
                "RAPID_DOC_MYSQL_HOST": "mysql.internal",
                "RAPID_DOC_MYSQL_PORT": "3307",
                "RAPID_DOC_MYSQL_DATABASE": "rapid_doc_prod",
                "RAPID_DOC_MYSQL_USER": "rapid_doc_app",
                "RAPID_DOC_MYSQL_PASSWORD": "secret",
                "RAPID_DOC_MYSQL_POOL_SIZE": "8",
            }
        )

        self.assertEqual(settings.database_backend, "mysql")
        self.assertEqual(settings.mysql_host, "mysql.internal")
        self.assertEqual(settings.mysql_port, 3307)
        self.assertEqual(settings.mysql_database, "rapid_doc_prod")
        self.assertEqual(settings.mysql_user, "rapid_doc_app")
        self.assertEqual(settings.mysql_password, "secret")
        self.assertEqual(settings.mysql_pool_size, 8)
        self.assertEqual(settings.database_backend, "mysql")

    def test_unknown_database_backend_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be mysql"):
            JobSettings.from_env({"RAPID_DOC_DB_BACKEND": "postgres"})

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

    def test_database_creates_required_tables_and_indexes(self) -> None:
        settings = mysql_test_settings(self.root / "jobs")
        connection = connect_database(settings)
        try:
            tables = {
                row["TABLE_NAME"]
                for row in connection.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE()"
                )
            }
            indexes = {
                row["INDEX_NAME"]
                for row in connection.execute(
                    "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA = DATABASE()"
                )
            }
            job_columns = {
                row["COLUMN_NAME"]
                for row in connection.execute(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jobs'"
                )
            }
        finally:
            connection.close()

        self.assertTrue({"jobs", "parse_cache", "callback_outbox", "service_heartbeats"} <= tables)
        self.assertIn("jobs_tenant_idempotency_key_unique", indexes)
        self.assertIn("parse_cache_state_expires_at_index", indexes)
        self.assertEqual(len(job_columns), 28)
        self.assertFalse(
            {
                "source_extension",
                "input_path",
                "result_source",
                "worker_id",
                "result_bytes",
            }
            & job_columns
        )

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
