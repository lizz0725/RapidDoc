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
from rapid_doc.jobs.job_types import CacheState


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
