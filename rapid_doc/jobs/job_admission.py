"""异步 Job 创建接口的文件准入与落盘逻辑。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import UploadFile
from pypdf import PdfReader

from .job_artifacts import ArtifactStore
from .job_config import JobSettings
from .job_database import initialize_database
from .job_limits import JobAdmissionLimits
from .job_store import JobCreation, JobStore, JobSubmission
from .job_types import generate_ulid


_CHUNK_SIZE = 1024 * 1024
_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
_MIME_TYPES = {
    "pdf": {"application/pdf"},
    "doc": {"application/msword"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
    "xls": {"application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "png": {"image/png"},
    "jpg": {"image/jpeg", "image/jpg"},
    "tiff": {"image/tiff"},
}
_EXTENSION_ALIASES = {"jpeg": "jpg", "jpg": "jpg", "tif": "tiff", "tiff": "tiff"}


class JobAdmissionError(Exception):
    """文件准入失败时返回给调用方的结构化错误。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class JobAdmissionService:
    """在不加载 OCR 模型的前提下完成上传准入、落盘和 Job 创建。"""

    def __init__(self, settings: JobSettings) -> None:
        self.settings = settings
        self.artifacts = ArtifactStore(settings.data_dir)
        self.store = JobStore(settings.database_path, settings)
        # 单容器第一期用进程内短锁保护磁盘预算的逐块检查与写入。
        self._storage_lock = asyncio.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.artifacts.ensure_layout()
        initialize_database(self.settings.database_path)
        self._initialized = True

    async def create_job(
        self,
        *,
        upload: UploadFile,
        tenant_id: str | None,
        business_ref: str | None,
        callback_url: str | None,
        idempotency_key: str | None,
    ) -> JobCreation:
        self.initialize()
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        normalized_business_ref = _normalize_text(
            business_ref,
            field_name="businessRef",
            error_code="INVALID_BUSINESS_REF",
            required=False,
        )
        normalized_callback_url = _normalize_callback_url(callback_url)
        normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
        source_filename, uploaded_extension = _validate_uploaded_filename(
            upload.filename, self.settings.allowed_extensions
        )

        staging_path = self.artifacts.staging_path(generate_ulid())
        destination: Path | None = None
        persisted = False
        try:
            source_bytes, source_sha256 = await self._stream_to_staging(upload, staging_path)
            actual_extension = _detect_file_extension(staging_path, uploaded_extension)
            if actual_extension is None or not _extension_is_allowed(
                actual_extension, self.settings.allowed_extensions
            ):
                raise JobAdmissionError(
                    415,
                    "UNSUPPORTED_FILE_TYPE",
                    "文件内容不是当前 Job API 支持的 PDF、Office 或图片格式。",
                )
            _validate_content_type(upload.content_type, actual_extension)
            stored_filename = _stored_filename(source_filename, actual_extension)
            page_metadata = _pdf_page_metadata(staging_path, actual_extension, self.settings)

            candidate_job_id = generate_ulid()
            destination = self.artifacts.input_path(candidate_job_id, stored_filename)
            self.artifacts.publish(staging_path, destination)
            submission = JobSubmission(
                tenant_id=normalized_tenant_id,
                request_fingerprint=_request_fingerprint(
                    normalized_tenant_id, source_sha256, normalized_callback_url
                ),
                source_filename=source_filename,
                stored_filename=stored_filename,
                source_sha256=source_sha256,
                source_bytes=source_bytes,
                business_ref=normalized_business_ref,
                callback_url=normalized_callback_url,
                idempotency_key=normalized_idempotency_key,
                **page_metadata,
            )
            creation = self.store.create_or_reuse_job(submission, job_id=candidate_job_id)
            if creation.reused_idempotency_key:
                self.artifacts.remove_tree(destination.parent)
            else:
                persisted = True
            return creation
        except Exception:
            if destination is not None and not persisted:
                self.artifacts.remove_tree(destination.parent)
            raise
        finally:
            self.artifacts.remove_file(staging_path)

    async def _stream_to_staging(self, upload: UploadFile, staging_path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        total_bytes = 0
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with staging_path.open("xb") as output:
                while chunk := await upload.read(_CHUNK_SIZE):
                    async with self._storage_lock:
                        if (
                            self.artifacts.retained_bytes() + len(chunk)
                            > JobAdmissionLimits.MAX_RETAINED_BYTES
                        ):
                            raise JobAdmissionError(
                                429,
                                "STORAGE_CAPACITY_EXCEEDED",
                                "任务数据目录已达到第一期固定磁盘预算。",
                            )
                        if total_bytes + len(chunk) > self.settings.max_file_size_bytes:
                            raise JobAdmissionError(
                                413,
                                "FILE_TOO_LARGE",
                                "文件大小超过 RAPID_DOC_MAX_FILE_SIZE_MB 的限制。",
                            )
                        output.write(chunk)
                    digest.update(chunk)
                    total_bytes += len(chunk)
                output.flush()
                os.fsync(output.fileno())
        finally:
            await upload.close()
        return total_bytes, digest.hexdigest()


def _normalize_text(
    value: str | None, *, field_name: str, error_code: str, required: bool
) -> str | None:
    if value is None:
        if required:
            raise JobAdmissionError(422, error_code, f"{field_name} 为必填字段。")
        return None
    normalized = value.strip()
    if not normalized:
        if required:
            raise JobAdmissionError(422, error_code, f"{field_name} 为必填字段。")
        return None
    if len(normalized) > 128 or not normalized.isprintable():
        raise JobAdmissionError(422, error_code, f"{field_name} 必须是不超过 128 个字符的可打印文本。")
    return normalized


def normalize_tenant_id(value: str | None) -> str:
    """统一创建、查询、获取结果和取消接口的租户参数校验。"""

    normalized = _normalize_text(
        value, field_name="tenantId", error_code="INVALID_TENANT_ID", required=True
    )
    assert normalized is not None
    return normalized


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 256 or not normalized.isprintable():
        raise JobAdmissionError(
            422,
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key 必须是不超过 256 个字符的可打印文本。",
        )
    return normalized


def _normalize_callback_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if len(value) > 2048:
        raise JobAdmissionError(422, "INVALID_CALLBACK_URL", "callbackUrl 长度不能超过 2048。")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise JobAdmissionError(
            422,
            "INVALID_CALLBACK_URL",
            "callbackUrl 必须是包含主机名的 HTTP 或 HTTPS 地址。",
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))


def _validate_uploaded_filename(
    filename: str | None, allowed_extensions: frozenset[str]
) -> tuple[str, str]:
    if not filename:
        raise JobAdmissionError(415, "UNSUPPORTED_FILE_TYPE", "上传文件必须包含文件名和扩展名。")
    source_filename = _sanitize_filename(filename)
    extension = Path(source_filename).suffix.lower().lstrip(".")
    if not extension or not _extension_is_allowed(extension, allowed_extensions):
        raise JobAdmissionError(
            415,
            "UNSUPPORTED_FILE_TYPE",
            "文件扩展名不在 RAPID_DOC_ALLOWED_EXTENSIONS 白名单中。",
        )
    return source_filename, _canonical_extension(extension)


def _sanitize_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    sanitized = re.sub(r"[^\w.-]", "_", basename, flags=re.UNICODE).lstrip(".")
    if not sanitized:
        return "upload"
    return sanitized[:255]


def _stored_filename(source_filename: str, actual_extension: str) -> str:
    stem = Path(source_filename).stem.rstrip(".") or "upload"
    safe_stem = re.sub(r"[^\w-]", "_", stem, flags=re.UNICODE).strip("_") or "upload"
    return f"{safe_stem[:200]}.{actual_extension}"


def _canonical_extension(extension: str) -> str:
    return _EXTENSION_ALIASES.get(extension.lower(), extension.lower())


def _extension_is_allowed(extension: str, allowed_extensions: frozenset[str]) -> bool:
    canonical_allowed = {_canonical_extension(item) for item in allowed_extensions}
    return _canonical_extension(extension) in canonical_allowed


def _detect_file_extension(path: Path, uploaded_extension: str) -> str | None:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if header.startswith(_OLE_HEADER):
        return uploaded_extension if uploaded_extension in {"doc", "xls"} else None
    if not header.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            members = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    if "[Content_Types].xml" not in members:
        return None
    if any(member.startswith("word/") for member in members):
        return "docx"
    if any(member.startswith("xl/") for member in members):
        return "xlsx"
    return None


def _validate_content_type(content_type: str | None, actual_extension: str) -> None:
    declared = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if declared in _GENERIC_MIME_TYPES:
        return
    if declared not in _MIME_TYPES[actual_extension]:
        raise JobAdmissionError(
            415,
            "UNSUPPORTED_FILE_TYPE",
            "上传请求声明的 MIME 类型与文件实际内容不一致。",
        )


def _pdf_page_metadata(
    path: Path, extension: str, settings: JobSettings
) -> dict[str, Any]:
    if extension != "pdf":
        return {
            "source_page_count": None,
            "processed_page_count": None,
            "truncated": False,
            "warnings": None,
        }
    try:
        source_page_count = len(PdfReader(path, strict=False).pages)
    except Exception as exc:
        raise JobAdmissionError(422, "INVALID_PDF", "上传的 PDF 无法读取页数。") from exc
    processed_page_count = min(source_page_count, settings.max_pdf_pages)
    truncated = source_page_count > settings.max_pdf_pages
    warnings = None
    if truncated:
        warnings = [
            {
                "code": "PDF_PAGE_LIMIT_TRUNCATED",
                "message": (
                    f"PDF 共 {source_page_count} 页，已按配置仅处理前 "
                    f"{processed_page_count} 页。"
                ),
            }
        ]
    return {
        "source_page_count": source_page_count,
        "processed_page_count": processed_page_count,
        "truncated": truncated,
        "warnings": warnings,
    }


def _request_fingerprint(tenant_id: str, source_sha256: str, callback_url: str | None) -> str:
    payload = json.dumps(
        {
            "callbackUrl": callback_url,
            "sourceSha256": source_sha256,
            "tenantId": tenant_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
