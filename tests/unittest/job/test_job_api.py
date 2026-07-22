"""异步 Job 创建接口的准入测试。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from rapid_doc.jobs.job_admission import JobAdmissionService
from rapid_doc.jobs.job_config import JobSettings
from rapid_doc.jobs.job_database import connect_database
from rapid_doc.jobs.job_limits import JobAdmissionLimits
from rapid_doc.jobs.job_runtime import (
    CALLBACK_DISPATCHER_COMPONENT,
    MAINTENANCE_COMPONENT,
    OCR_WORKER_COMPONENT,
    JobRuntime,
)
from rapid_doc.jobs.job_types import CacheRole, CacheState, JobState


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKER_DIR = REPOSITORY_ROOT / "docker"
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

import app as docker_app  # noqa: E402


class JobApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = JobSettings(data_dir=self.root / "jobs", max_pdf_pages=1)
        self.service = JobAdmissionService(self.settings)
        docker_app.app.state.rapid_doc_job_service = self.service
        self.addCleanup(self._clear_job_service)
        self.client = TestClient(docker_app.app)

    def test_job_modules_remain_compatible_with_python_310_image(self) -> None:
        """镜像基于 Python 3.10，不能使用 3.11 才新增的 datetime.UTC。"""
        job_directory = REPOSITORY_ROOT / "rapid_doc" / "jobs"

        for module_name in ("job_api.py", "job_callback.py", "job_store.py"):
            with self.subTest(module=module_name):
                source = (job_directory / module_name).read_text(encoding="utf-8")
                self.assertNotIn("from datetime import UTC", source)
                self.assertNotIn("datetime.now(UTC)", source)
                self.assertNotIn("fromtimestamp(timestamp, UTC)", source)
                self.assertIn("timezone.utc", source)

    @staticmethod
    def pdf_bytes(page_count: int = 1) -> bytes:
        output = io.BytesIO()
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=72, height=72)
        writer.write(output)
        return output.getvalue()

    @staticmethod
    def png_bytes() -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"fixture"

    @staticmethod
    def ooxml_bytes(member: str) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr(member, "<fixture />")
        return output.getvalue()

    def _clear_job_service(self) -> None:
        if getattr(docker_app.app.state, "rapid_doc_job_service", None) is self.service:
            del docker_app.app.state.rapid_doc_job_service

    def assert_no_enqueued_job_or_input(self) -> None:
        connection = connect_database(self.settings.database_path)
        try:
            job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(job_count, 0)
        self.assertFalse(list((self.settings.data_dir / "inputs").rglob("*")))

    def publish_success_result(self, job_id: str, markdown: str) -> None:
        job = self.service.store.get_job("finance", job_id)
        self.assertIsNotNone(job)
        assert job is not None
        result_path = self.service.artifacts.cache_result_path(
            "finance", job["source_sha256"], "json"
        )
        self.service.artifacts.write_bytes_atomic(
            result_path,
            json.dumps(
                {"markdown": markdown, "metadata": {"engine": "fixture"}},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        relative_path = str(result_path.relative_to(self.settings.data_dir))
        connection = connect_database(self.settings.database_path)
        try:
            connection.execute(
                """
                UPDATE jobs
                SET job_state = ?, result_path = ?, finished_at = ?, result_expires_at = ?
                WHERE job_id = ?
                """,
                (JobState.SUCCEEDED.value, relative_path, 1_700_000_010, 1_700_600_000, job_id),
            )
            connection.execute(
                """
                UPDATE parse_cache
                SET cache_state = ?, result_path = ?
                WHERE tenant_id = ? AND source_sha256 = ?
                """,
                (CacheState.READY.value, relative_path, "finance", job["source_sha256"]),
            )
        finally:
            connection.close()

    def test_create_pdf_job_persists_input_and_page_warning(self) -> None:
        source = self.pdf_bytes(page_count=2)

        response = self.client.post(
            "/jobs",
            files={"file": ("合同.pdf", source, "application/pdf")},
            data={"tenantId": "finance", "businessRef": "contract-001"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["jobState"], "queued")
        self.assertEqual(payload["callbackState"], "not_requested")
        self.assertEqual(payload["cache"], {"role": "owner", "resultSource": None})
        job = self.service.store.get_job("finance", payload["jobId"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["stored_filename"], "合同.pdf")
        self.assertEqual(job["source_page_count"], 2)
        self.assertEqual(job["processed_page_count"], 1)
        self.assertEqual(job["truncated"], 1)
        self.assertEqual(json.loads(job["warnings_json"])[0]["code"], "PDF_PAGE_LIMIT_TRUNCATED")
        input_path = self.service.artifacts.input_path(payload["jobId"], job["stored_filename"])
        self.assertEqual(input_path.read_bytes(), source)

    def test_actual_image_type_corrects_uploaded_suffix(self) -> None:
        response = self.client.post(
            "/jobs",
            files={"file": ("scan.pdf", self.png_bytes(), "image/png")},
            data={"tenantId": "finance"},
        )

        self.assertEqual(response.status_code, 202)
        job = self.service.store.get_job("finance", response.json()["jobId"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["source_filename"], "scan.pdf")
        self.assertEqual(job["stored_filename"], "scan.png")
        self.assertIsNone(job["source_page_count"])
        self.assertEqual(job["truncated"], 0)

    def test_create_job_accepts_docx_and_xlsx_packages(self) -> None:
        fixtures = (
            (
                "proposal.docx",
                self.ooxml_bytes("word/document.xml"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "docx",
            ),
            (
                "budget.xlsx",
                self.ooxml_bytes("xl/workbook.xml"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "xlsx",
            ),
        )

        for filename, source, content_type, expected_extension in fixtures:
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/jobs",
                    files={"file": (filename, source, content_type)},
                    data={"tenantId": "finance"},
                )

                self.assertEqual(response.status_code, 202)
                job = self.service.store.get_job("finance", response.json()["jobId"])
                self.assertIsNotNone(job)
                assert job is not None
                self.assertEqual(Path(job["stored_filename"]).suffix, f".{expected_extension}")

    def test_idempotency_key_reuses_existing_job_without_duplicate_input(self) -> None:
        source = self.pdf_bytes()
        request = {
            "files": {"file": ("contract.pdf", source, "application/pdf")},
            "data": {"tenantId": "finance"},
            "headers": {"Idempotency-Key": "contract-import-001"},
        }

        first = self.client.post("/jobs", **request)
        second = self.client.post("/jobs", **request)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["jobId"], second.json()["jobId"])
        input_directories = list((self.settings.data_dir / "inputs").iterdir())
        self.assertEqual(len(input_directories), 1)

    def test_ready_cache_returns_completed_hit_with_derived_result_source(self) -> None:
        source = self.pdf_bytes()
        first = self.client.post(
            "/jobs",
            files={"file": ("contract.pdf", source, "application/pdf")},
            data={"tenantId": "finance"},
        )
        connection = connect_database(self.settings.database_path)
        try:
            connection.execute(
                """
                UPDATE parse_cache
                SET cache_state = ?, result_path = ?
                WHERE tenant_id = ? AND owner_job_id = ?
                """,
                (
                    CacheState.READY.value,
                    "cache/finance/result.md",
                    "finance",
                    first.json()["jobId"],
                ),
            )
        finally:
            connection.close()

        hit = self.client.post(
            "/jobs",
            files={"file": ("duplicate.pdf", source, "application/pdf")},
            data={"tenantId": "finance"},
        )

        self.assertEqual(hit.status_code, 202)
        self.assertEqual(hit.json()["jobState"], "succeeded")
        self.assertEqual(hit.json()["cache"], {"role": "hit", "resultSource": "cache"})

    def test_status_reports_dynamic_queue_observation_and_hides_other_tenants(self) -> None:
        first = self.client.post(
            "/jobs",
            files={"file": ("first.pdf", self.pdf_bytes(page_count=1), "application/pdf")},
            data={"tenantId": "finance"},
        )
        second = self.client.post(
            "/jobs",
            files={"file": ("second.pdf", self.pdf_bytes(page_count=2), "application/pdf")},
            data={"tenantId": "finance"},
        )

        response = self.client.get(
            f"/jobs/{second.json()['jobId']}", headers={"X-Tenant-Id": "finance"}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["jobState"], JobState.QUEUED.value)
        self.assertEqual(payload["queueSeq"], 2)
        self.assertEqual(payload["queuePosition"], 2)
        self.assertEqual(payload["aheadQueuedCount"], 1)
        self.assertEqual(payload["runningJobCount"], 0)
        self.assertEqual(payload["workerCapacity"], 1)
        self.assertEqual(payload["sourcePageCount"], 2)
        self.assertEqual(payload["processedPageCount"], 1)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["warnings"][0]["code"], "PDF_PAGE_LIMIT_TRUNCATED")
        self.assertEqual(payload["error"], None)
        denied = self.client.get(
            f"/jobs/{first.json()['jobId']}", headers={"X-Tenant-Id": "other"}
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.json()["error"]["code"], "JOB_NOT_FOUND")

    def test_operations_queue_returns_only_fifo_owners_and_ready_requires_components(self) -> None:
        first = self.client.post(
            "/jobs",
            files={"file": ("first.pdf", self.pdf_bytes(page_count=1), "application/pdf")},
            data={"tenantId": "finance"},
        )
        second = self.client.post(
            "/jobs",
            files={"file": ("second.pdf", self.pdf_bytes(page_count=2), "application/pdf")},
            data={"tenantId": "legal"},
        )
        self.client.post(
            "/jobs",
            files={"file": ("duplicate.pdf", self.pdf_bytes(page_count=1), "application/pdf")},
            data={"tenantId": "finance"},
        )

        queue = self.client.get("/ops/jobs/queue")

        self.assertEqual(queue.status_code, 200)
        payload = queue.json()
        self.assertEqual(payload["queuedCount"], 2)
        self.assertEqual(payload["runningJobCount"], 0)
        self.assertEqual(payload["workerCapacity"], 1)
        self.assertEqual(
            [item["jobId"] for item in payload["items"]],
            [first.json()["jobId"], second.json()["jobId"]],
        )
        self.assertEqual([item["queuePosition"] for item in payload["items"]], [1, 2])
        self.assertEqual(payload["items"][0]["tenantId"], "finance")

        self.assertEqual(self.client.get("/health/live").json()["status"], "alive")
        self.assertEqual(self.client.get("/health/ready").status_code, 503)

        runtime = JobRuntime(self.settings)
        for component_type, component_id in (
            (OCR_WORKER_COMPONENT, "ocr-worker-1"),
            (MAINTENANCE_COMPONENT, "maintenance-main"),
            (CALLBACK_DISPATCHER_COMPONENT, "callback-dispatcher-main"),
        ):
            runtime.record_heartbeat(component_type, component_id)

        ready = self.client.get("/health/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertTrue(ready.json()["checks"]["components"][OCR_WORKER_COMPONENT]["ready"])

    def test_result_endpoint_reports_pending_success_and_expiry(self) -> None:
        created = self.client.post(
            "/jobs",
            files={"file": ("contract.pdf", self.pdf_bytes(page_count=2), "application/pdf")},
            data={"tenantId": "finance"},
        )
        job_id = created.json()["jobId"]
        pending = self.client.get(f"/jobs/{job_id}/result", headers={"X-Tenant-Id": "finance"})
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(
            pending.json(),
            {"jobId": job_id, "jobState": JobState.QUEUED.value, "result": None},
        )

        self.publish_success_result(job_id, "# 合同\n\n正文")
        succeeded = self.client.get(
            f"/jobs/{job_id}/result", headers={"X-Tenant-Id": "finance"}
        )
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(succeeded.json()["result"]["markdown"], "# 合同\n\n正文")
        self.assertEqual(succeeded.json()["result"]["metadata"]["engine"], "fixture")
        self.assertEqual(succeeded.json()["result"]["metadata"]["resultSource"], "ocr")
        self.assertEqual(succeeded.json()["result"]["metadata"]["sourcePageCount"], 2)
        self.assertTrue(succeeded.json()["result"]["metadata"]["truncated"])

        connection = connect_database(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE jobs SET job_state = ? WHERE job_id = ?",
                (JobState.RESULT_EXPIRED.value, job_id),
            )
        finally:
            connection.close()
        expired = self.client.get(f"/jobs/{job_id}/result", headers={"X-Tenant-Id": "finance"})
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.json()["error"]["code"], "RESULT_EXPIRED")

    def test_cancel_owner_promotes_follower_and_running_job_cannot_be_cancelled(self) -> None:
        source = self.pdf_bytes()
        owner = self.client.post(
            "/jobs",
            files={"file": ("contract.pdf", source, "application/pdf")},
            data={"tenantId": "finance"},
        )
        follower = self.client.post(
            "/jobs",
            files={"file": ("duplicate.pdf", source, "application/pdf")},
            data={"tenantId": "finance"},
        )

        cancelled = self.client.post(
            f"/jobs/{owner.json()['jobId']}/cancel", headers={"X-Tenant-Id": "finance"}
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["jobState"], JobState.CANCELLED.value)
        follower_status = self.client.get(
            f"/jobs/{follower.json()['jobId']}", headers={"X-Tenant-Id": "finance"}
        )
        self.assertEqual(follower_status.status_code, 200)
        self.assertEqual(follower_status.json()["jobState"], JobState.QUEUED.value)
        self.assertEqual(follower_status.json()["cache"]["role"], CacheRole.OWNER.value)
        self.assertEqual(follower_status.json()["queuePosition"], 1)
        unavailable = self.client.get(
            f"/jobs/{owner.json()['jobId']}/result", headers={"X-Tenant-Id": "finance"}
        )
        self.assertEqual(unavailable.status_code, 409)

        self.assertIsNotNone(
            self.service.store.claim_next_job(attempt_token="fixture-attempt")
        )
        cannot_cancel = self.client.post(
            f"/jobs/{follower.json()['jobId']}/cancel", headers={"X-Tenant-Id": "finance"}
        )
        self.assertEqual(cannot_cancel.status_code, 409)
        self.assertEqual(cannot_cancel.json()["error"]["code"], "JOB_CANNOT_BE_CANCELLED")

    def test_create_job_rejects_oversized_file_before_enqueue(self) -> None:
        oversized = b"x" * (self.settings.max_file_size_bytes + 1)

        response = self.client.post(
            "/jobs",
            files={"file": ("large.pdf", oversized, "application/pdf")},
            data={"tenantId": "finance"},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "FILE_TOO_LARGE")
        self.assert_no_enqueued_job_or_input()

    def test_create_job_rejects_unsupported_extension_without_enqueue(self) -> None:
        response = self.client.post(
            "/jobs",
            files={"file": ("malware.exe", b"fixture", "application/octet-stream")},
            data={"tenantId": "finance"},
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["error"]["code"], "UNSUPPORTED_FILE_TYPE")
        self.assert_no_enqueued_job_or_input()

    def test_create_job_rejects_mismatched_declared_mime_type(self) -> None:
        response = self.client.post(
            "/jobs",
            files={"file": ("contract.pdf", self.pdf_bytes(), "image/png")},
            data={"tenantId": "finance"},
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["error"]["code"], "UNSUPPORTED_FILE_TYPE")

    def test_create_job_rejects_when_fixed_storage_budget_is_exhausted(self) -> None:
        with patch.object(JobAdmissionLimits, "MAX_RETAINED_BYTES", 1):
            response = self.client.post(
                "/jobs",
                files={"file": ("contract.pdf", self.pdf_bytes(), "application/pdf")},
                data={"tenantId": "finance"},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "STORAGE_CAPACITY_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
