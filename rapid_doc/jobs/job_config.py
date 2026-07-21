"""从环境变量读取异步 Job 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_ALLOWED_EXTENSIONS = frozenset(
    {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg", "tif", "tiff"}
)
DEFAULT_JOB_DATA_DIR = Path("/app/output/jobs")


def _read_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _read_positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _read_extensions(environ: Mapping[str, str]) -> frozenset[str]:
    raw_value = environ.get("RAPID_DOC_ALLOWED_EXTENSIONS")
    if raw_value is None:
        return DEFAULT_ALLOWED_EXTENSIONS
    extensions = frozenset(
        value.strip().lower().lstrip(".")
        for value in raw_value.split(",")
        if value.strip()
    )
    if not extensions:
        raise ValueError("RAPID_DOC_ALLOWED_EXTENSIONS must not be empty")
    if any(not extension.replace("_", "").isalnum() for extension in extensions):
        raise ValueError("RAPID_DOC_ALLOWED_EXTENSIONS contains an invalid extension")
    return extensions


def _read_optional_secret(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True)
class JobSettings:
    async_enabled: bool = True
    worker_processes: int = 1
    max_file_size_mb: int = 100
    allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS
    max_pdf_pages: int = 100
    queue_expire_minutes: int = 43_200
    result_ttl_minutes: int = 10_080
    cache_ttl_minutes: int = 43_200
    max_run_minutes: int = 60
    tombstone_ttl_minutes: int = 43_200
    max_processing_attempts: int = 2
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    watchdog_interval_seconds: int = 10
    sweeper_interval_seconds: int = 60
    callback_connect_timeout_seconds: int = 5
    callback_read_timeout_seconds: int = 30
    callback_signing_secret: str | None = None
    data_dir: Path = DEFAULT_JOB_DATA_DIR

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "JobSettings":
        environ = os.environ if environ is None else environ
        settings = cls(
            async_enabled=_read_bool(environ, "RAPID_DOC_ASYNC_ENABLED", True),
            worker_processes=_read_positive_int(environ, "RAPID_DOC_WORKER_PROCESSES", 1),
            max_file_size_mb=_read_positive_int(environ, "RAPID_DOC_MAX_FILE_SIZE_MB", 100),
            allowed_extensions=_read_extensions(environ),
            max_pdf_pages=_read_positive_int(environ, "RAPID_DOC_MAX_PDF_PAGES", 100),
            queue_expire_minutes=_read_positive_int(environ, "RAPID_DOC_QUEUE_EXPIRE_MINUTES", 43_200),
            result_ttl_minutes=_read_positive_int(environ, "RAPID_DOC_RESULT_TTL_MINUTES", 10_080),
            cache_ttl_minutes=_read_positive_int(environ, "RAPID_DOC_CACHE_TTL_MINUTES", 43_200),
            max_run_minutes=_read_positive_int(environ, "RAPID_DOC_JOB_MAX_RUN_MINUTES", 60),
            tombstone_ttl_minutes=_read_positive_int(environ, "RAPID_DOC_TOMBSTONE_TTL_MINUTES", 43_200),
            max_processing_attempts=_read_positive_int(
                environ, "RAPID_DOC_JOB_MAX_PROCESSING_ATTEMPTS", 2
            ),
            lease_seconds=_read_positive_int(environ, "RAPID_DOC_JOB_LEASE_SECONDS", 120),
            heartbeat_seconds=_read_positive_int(
                environ, "RAPID_DOC_JOB_HEARTBEAT_SECONDS", 30
            ),
            watchdog_interval_seconds=_read_positive_int(
                environ, "RAPID_DOC_MAINTENANCE_WATCHDOG_INTERVAL_SECONDS", 10
            ),
            sweeper_interval_seconds=_read_positive_int(
                environ, "RAPID_DOC_MAINTENANCE_SWEEPER_INTERVAL_SECONDS", 60
            ),
            callback_connect_timeout_seconds=_read_positive_int(
                environ, "RAPID_DOC_CALLBACK_CONNECT_TIMEOUT_SECONDS", 5
            ),
            callback_read_timeout_seconds=_read_positive_int(
                environ, "RAPID_DOC_CALLBACK_READ_TIMEOUT_SECONDS", 30
            ),
            callback_signing_secret=_read_optional_secret(
                environ, "RAPID_DOC_CALLBACK_SIGNING_SECRET"
            ),
            data_dir=Path(environ.get("RAPID_DOC_JOB_DATA_DIR", str(DEFAULT_JOB_DATA_DIR))),
        )
        settings.validate()
        return settings

    @property
    def database_path(self) -> Path:
        return self.data_dir / "rapid-doc.db"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def queue_expire_seconds(self) -> int:
        return self.queue_expire_minutes * 60

    @property
    def result_ttl_seconds(self) -> int:
        return self.result_ttl_minutes * 60

    @property
    def cache_ttl_seconds(self) -> int:
        return self.cache_ttl_minutes * 60

    @property
    def max_run_seconds(self) -> int:
        return self.max_run_minutes * 60

    @property
    def tombstone_ttl_seconds(self) -> int:
        return self.tombstone_ttl_minutes * 60

    @property
    def background_heartbeat_fresh_seconds(self) -> int:
        """后台进程超过三个心跳周期未上报即视为不健康，最低保留一分钟。"""

        return max(60, self.heartbeat_seconds * 3)

    def validate(self) -> None:
        if self.cache_ttl_minutes < self.result_ttl_minutes:
            raise ValueError("RAPID_DOC_CACHE_TTL_MINUTES must be at least RAPID_DOC_RESULT_TTL_MINUTES")
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("RAPID_DOC_JOB_HEARTBEAT_SECONDS must be less than RAPID_DOC_JOB_LEASE_SECONDS")
