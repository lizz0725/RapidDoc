"""异步 Job 运行时心跳与就绪检查测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_runtime import (
    CALLBACK_DISPATCHER_COMPONENT,
    MAINTENANCE_COMPONENT,
    OCR_WORKER_COMPONENT,
    JobRuntime,
)
from tests.unittest.job.test_support import mysql_test_settings


class JobRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = mysql_test_settings(self.root / "jobs", worker_processes=2)
        self.runtime = JobRuntime(self.settings)
        self.now = 1_700_000_000

    def test_readiness_requires_all_background_components_and_rejects_stale_heartbeats(self) -> None:
        missing = self.runtime.readiness(now=self.now)
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["checks"]["components"][OCR_WORKER_COMPONENT]["ready"])

        for component_type, component_id in (
            (OCR_WORKER_COMPONENT, "ocr-worker-1"),
            (OCR_WORKER_COMPONENT, "ocr-worker-2"),
            (MAINTENANCE_COMPONENT, "maintenance-main"),
            (CALLBACK_DISPATCHER_COMPONENT, "callback-dispatcher-main"),
        ):
            self.runtime.record_heartbeat(component_type, component_id, now=self.now)

        ready = self.runtime.readiness(now=self.now)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["checks"]["components"][OCR_WORKER_COMPONENT]["healthy"], 2)

        stale = self.runtime.readiness(
            now=self.now + self.settings.background_heartbeat_fresh_seconds + 1
        )
        self.assertFalse(stale["ready"])
        self.assertEqual(stale["checks"]["components"][OCR_WORKER_COMPONENT]["healthy"], 0)

    def test_disabled_async_jobs_do_not_require_background_component_heartbeats(self) -> None:
        runtime = JobRuntime(
            mysql_test_settings(self.root / "disabled", async_enabled=False)
        )

        report = runtime.readiness(now=self.now)

        self.assertTrue(report["ready"])
        self.assertEqual(report["checks"]["components"], {})


if __name__ == "__main__":
    unittest.main()
