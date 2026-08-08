"""将容器内所有组件 stdout/stderr 统一写入按日期轮转的日志文件。"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 15


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _log_dir() -> Path:
    data_dir = Path(os.environ.get("RAPID_DOC_JOB_DATA_DIR", "/app/output/jobs"))
    path = Path(os.environ.get("RAPID_DOC_LOG_DIR", str(data_dir / "logs")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old_logs(log_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now().timestamp() - timedelta(days=retention_days).total_seconds()
    for path in log_dir.glob("rapid-doc-*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _current_log_path(log_dir: Path, max_bytes: int) -> Path:
    date_text = datetime.now().strftime("%Y-%m-%d")
    base = log_dir / f"rapid-doc-{date_text}.log"
    try:
        if not base.exists() or base.stat().st_size < max_bytes:
            return base
    except OSError:
        return base

    index = 1
    while True:
        candidate = log_dir / f"rapid-doc-{date_text}.{index}.log"
        try:
            if not candidate.exists() or candidate.stat().st_size < max_bytes:
                return candidate
        except OSError:
            return candidate
        index += 1


def main() -> None:
    log_dir = _log_dir()
    max_bytes = _int_env("RAPID_DOC_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
    retention_days = _int_env("RAPID_DOC_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    _cleanup_old_logs(log_dir, retention_days)

    output = sys.stdout.buffer
    current_path: Path | None = None
    current_file = None
    last_cleanup_date = datetime.now().date()

    try:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                break

            today = datetime.now().date()
            if today != last_cleanup_date:
                _cleanup_old_logs(log_dir, retention_days)
                last_cleanup_date = today

            path = _current_log_path(log_dir, max_bytes)
            if path != current_path:
                if current_file is not None:
                    current_file.close()
                current_path = path
                current_file = path.open("ab", buffering=0)

            output.write(line)
            output.flush()
            current_file.write(line)
    finally:
        if current_file is not None:
            current_file.close()


if __name__ == "__main__":
    main()
