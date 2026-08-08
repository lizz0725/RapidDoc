"""容器统一日志写入器测试。"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def _load_log_writer_module():
    root = Path(__file__).resolve().parents[3]
    module_path = root / "docker" / "unified_log_writer.py"
    spec = importlib.util.spec_from_file_location("unified_log_writer", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载统一日志写入器。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnifiedLogWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.module = _load_log_writer_module()

    def test_current_log_path_uses_daily_file_and_numeric_suffix_when_file_is_full(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        base = self.root / f"rapid-doc-{today}.log"

        self.assertEqual(self.module._current_log_path(self.root, max_bytes=10), base)

        base.write_bytes(b"0123456789")
        first_suffix = self.root / f"rapid-doc-{today}.1.log"
        self.assertEqual(self.module._current_log_path(self.root, max_bytes=10), first_suffix)

        first_suffix.write_bytes(b"0123456789")
        second_suffix = self.root / f"rapid-doc-{today}.2.log"
        self.assertEqual(self.module._current_log_path(self.root, max_bytes=10), second_suffix)

    def test_cleanup_old_logs_only_removes_expired_rapid_doc_logs(self) -> None:
        expired = self.root / "rapid-doc-2026-01-01.log"
        recent = self.root / "rapid-doc-2026-01-02.log"
        unrelated = self.root / "other.log"
        for path in (expired, recent, unrelated):
            path.write_text("log", encoding="utf-8")

        old_time = (datetime.now() - timedelta(days=16)).timestamp()
        os.utime(expired, (old_time, old_time))

        self.module._cleanup_old_logs(self.root, retention_days=15)

        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
