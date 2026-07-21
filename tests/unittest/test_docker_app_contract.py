"""Regression coverage for the synchronous Docker API contract.

The async Job API must not alter the existing ``/file_parse`` behavior.  These
tests replace the OCR boundary with a tiny async stub, so they run without
models, LibreOffice, or network access.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPOSITORY_ROOT / "docker"
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

import app as docker_app  # noqa: E402


class DockerApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(docker_app.app)
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)

    def test_health_contract(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")
        self.assertEqual(response.json()["api"], "RapidDoc Web API")
        self.assertEqual(response.json()["compatible"], "Official RapidDoc API")

    def test_file_parse_returns_markdown_without_loading_models(self) -> None:
        captured: dict[str, object] = {}

        async def fake_aio_do_parse(**kwargs: object) -> None:
            captured.update(kwargs)
            output_dir = Path(str(kwargs["output_dir"]))
            pdf_name = str(kwargs["pdf_file_names"][0])
            parse_method = str(kwargs["parse_method"])
            result_dir = output_dir / pdf_name / parse_method
            result_dir.mkdir(parents=True)
            (result_dir / f"{pdf_name}.md").write_text(
                "# Contract fixture\n\nRecognized text.\n", encoding="utf-8"
            )

        with patch.object(docker_app, "aio_do_parse", new=fake_aio_do_parse):
            response = self.client.post(
                "/file_parse",
                files={"files": ("contract.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
                data={
                    "output_dir": self.output_dir.name,
                    "clear_output_file": "false",
                    "return_images": "false",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"]["contract"]["md_content"],
            "# Contract fixture\n\nRecognized text.\n",
        )
        self.assertNotIn("images", response.json()["results"]["contract"])
        self.assertEqual(captured["pdf_file_names"], ["contract"])
        self.assertEqual(captured["p_lang_list"], ["ch"])
        self.assertEqual(captured["backend"], "pipeline")
        self.assertEqual(captured["parse_method"], "auto")
        self.assertTrue(captured["formula_enable"])
        self.assertTrue(captured["table_enable"])
        self.assertEqual(captured["start_page_id"], 0)
        self.assertEqual(captured["end_page_id"], 99999)

    def test_file_parse_rejects_unsupported_file_type(self) -> None:
        response = self.client.post(
            "/file_parse",
            files={"files": ("unsupported.exe", b"fixture", "application/octet-stream")},
            data={"output_dir": self.output_dir.name},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not supported", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
