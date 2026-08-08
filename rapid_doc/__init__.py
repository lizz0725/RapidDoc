# Copyright (c) Opendatalab. All rights reserved.

from typing import TYPE_CHECKING

from rapid_doc.version import __version__

if TYPE_CHECKING:
    from rapid_doc.main import RapidDoc, RapidDocOutput


__all__ = ["RapidDoc", "RapidDocOutput", "__version__"]


def __getattr__(name: str):
    """按需加载完整 RapidDoc 主模块，避免后台 Job 进程提前加载 OCR 栈。"""

    if name == "RapidDoc":
        from rapid_doc.main import RapidDoc

        globals()[name] = RapidDoc
        return RapidDoc

    if name == "RapidDocOutput":
        from rapid_doc.main import RapidDocOutput

        globals()[name] = RapidDocOutput
        return RapidDocOutput

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
