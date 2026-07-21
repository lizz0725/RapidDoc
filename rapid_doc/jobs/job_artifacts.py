"""异步 Job 的文件目录布局与原子文件操作。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePath


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_layout(self) -> None:
        for path in (self.root, self.root / "inputs", self.root / "attempts", self.root / "cache"):
            path.mkdir(parents=True, exist_ok=True)

    def input_path(self, job_id: str, stored_filename: str) -> Path:
        return self.root / "inputs" / _safe_component(job_id) / _safe_filename(stored_filename)

    def attempt_result_path(self, job_id: str, attempt_token: str) -> Path:
        return (
            self.root
            / "attempts"
            / _safe_component(job_id)
            / _safe_component(attempt_token)
            / "result.json.tmp"
        )

    def cache_result_path(self, tenant_id: str, source_sha256: str, suffix: str) -> Path:
        if suffix not in {"json", "md"}:
            raise ValueError("cache result suffix must be json or md")
        return (
            self.root
            / "cache"
            / tenant_storage_key(tenant_id)
            / _safe_sha256(source_sha256)
            / f"result.{suffix}"
        )

    def write_bytes_atomic(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output_file:
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_path, destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def publish(self, temporary_path: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_path, destination)

    def remove_tree(self, path: Path) -> None:
        if self.root not in (path, *path.parents):
            raise ValueError("refusing to delete a path outside the artifact root")
        shutil.rmtree(path, ignore_errors=True)


def tenant_storage_key(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()


def _safe_component(value: str) -> str:
    if not value or PurePath(value).name != value or value in {".", ".."}:
        raise ValueError("path component is unsafe")
    return value


def _safe_filename(value: str) -> str:
    if not value or PurePath(value).name != value or value in {".", ".."}:
        raise ValueError("stored filename is unsafe")
    return value


def _safe_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    return value
