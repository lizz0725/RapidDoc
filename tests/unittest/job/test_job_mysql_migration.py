"""MySQL Schema 初始化、幂等和事务恢复测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_database import connect_database, initialize_database
from tests.unittest.job.test_support import mysql_test_settings


class JobMySQLMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = mysql_test_settings(Path(self.temporary_directory.name))

    def test_schema_initialization_is_idempotent_and_versioned(self) -> None:
        initialize_database(self.settings)
        connection = connect_database(self.settings)
        try:
            rows = connection.execute(
                "SELECT version, description FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual([row["version"] for row in rows], [1])
        self.assertEqual(rows[0]["description"], "MySQL 8 初始 Job Schema")

    def test_transaction_rollback_does_not_leave_partial_job_state(self) -> None:
        connection = connect_database(self.settings)
        try:
            connection.execute("START TRANSACTION")
            connection.execute(
                "INSERT INTO jobs ("
                "job_id, tenant_id, request_fingerprint, source_filename, stored_filename, "
                "source_sha256, source_bytes, job_state, cache_role, submitted_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "01HZZZZZZZZZZZZZZZZZZZZZZZ",
                    "migration-test",
                    "f" * 64,
                    "a.pdf",
                    "a.pdf",
                    "a" * 64,
                    1,
                    "queued",
                    "owner",
                    1_700_000_000,
                ),
            )
            connection.rollback()
        finally:
            connection.close()

        connection = connect_database(self.settings)
        try:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?",
                ("01HZZZZZZZZZZZZZZZZZZZZZZZ",),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
