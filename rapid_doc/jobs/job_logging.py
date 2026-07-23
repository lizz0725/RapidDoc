"""Job 进程的中文文件日志配置。"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger


_configured = False


def configure_file_logging(data_dir: str | Path | None = None) -> None:
    """为当前进程增加可挂载的滚动日志文件，同时保留标准输出日志。"""

    global _configured
    if _configured:
        return
    root = Path(data_dir or os.environ.get("RAPID_DOC_JOB_DATA_DIR", "/app/output/jobs"))
    log_dir = Path(os.environ.get("RAPID_DOC_LOG_DIR", str(root / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / f"rapid-doc-{os.getpid()}.log",
        rotation="100 MB",
        retention="15 days",
        encoding="utf-8",
        enqueue=False,
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {process} | {name}:{function}:{line} - {message}",
    )
    _configured = True
