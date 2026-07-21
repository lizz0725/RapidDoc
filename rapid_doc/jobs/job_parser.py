"""异步 Job Worker 对现有 RapidDoc 解析入口的固定策略适配。"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class JobParseError(RuntimeError):
    """输入转换或 RapidDoc 解析失败。"""


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    metadata: dict[str, object]


class RapidDocParseAdapter:
    """复用既有 ``aio_do_parse``，但不暴露同步接口的可变解析参数。"""

    def parse(
        self, job: dict[str, Any], input_path: Path, output_dir: Path
    ) -> ParsedDocument:
        if not input_path.is_file():
            raise FileNotFoundError(input_path)

        source_path = input_path
        converted_directory = output_dir / "converted"
        try:
            extension = source_path.suffix.lower().lstrip(".")
            if extension in {"doc", "xls"}:
                source_path = self._convert_legacy_office(source_path, converted_directory)
                extension = source_path.suffix.lower().lstrip(".")

            # 延迟导入：API 进程装配路由时不加载 OCR 相关模块或模型。
            from rapid_doc.cli.common import aio_do_parse, read_fn

            output_dir.mkdir(parents=True, exist_ok=True)
            document_name = Path(job["stored_filename"]).stem
            end_page_id = self._end_page_id(job, extension)
            source_bytes = read_fn(source_path, extension)
            asyncio.run(
                aio_do_parse(
                    output_dir=str(output_dir),
                    pdf_file_names=[document_name],
                    pdf_bytes_list=[source_bytes],
                    p_lang_list=["ch"],
                    backend="pipeline",
                    parse_method="auto",
                    formula_enable=False,
                    table_enable=True,
                    f_draw_layout_bbox=False,
                    f_draw_span_bbox=False,
                    f_dump_md=True,
                    f_dump_middle_json=False,
                    f_dump_model_output=False,
                    f_dump_orig_pdf=False,
                    f_dump_content_list=False,
                    start_page_id=0,
                    end_page_id=end_page_id,
                    layout_config={},
                    ocr_config={},
                    formula_config={},
                    table_config={},
                    checkbox_config={},
                    image_config={},
                )
            )
            markdown_path = self._markdown_path(output_dir, document_name, extension)
            if not markdown_path.is_file():
                raise JobParseError("RapidDoc 未生成 Markdown 结果文件。")
            return ParsedDocument(
                markdown=markdown_path.read_text(encoding="utf-8"),
                metadata={"backend": "pipeline", "parseMethod": "auto"},
            )
        except JobParseError:
            raise
        except Exception as exc:
            raise JobParseError(f"RapidDoc 解析失败：{exc}") from exc
        finally:
            # 转换中间文件仅属于当前 attempt，正式结果已由 Worker 单独发布。
            shutil.rmtree(converted_directory, ignore_errors=True)

    @staticmethod
    def _convert_legacy_office(input_path: Path, output_dir: Path) -> Path:
        from rapid_doc.utils.office_converter import convert_legacy_office_to_modern

        output_dir.mkdir(parents=True, exist_ok=True)
        return Path(convert_legacy_office_to_modern(input_path, output_dir))

    @staticmethod
    def _end_page_id(job: dict[str, Any], extension: str) -> int | None:
        if extension != "pdf" or job["processed_page_count"] is None:
            return None
        if job["processed_page_count"] <= 0:
            return None
        return job["processed_page_count"] - 1

    @staticmethod
    def _markdown_path(output_dir: Path, document_name: str, extension: str) -> Path:
        mode = "office" if extension in {"docx", "xlsx"} else "auto"
        return output_dir / document_name / mode / f"{document_name}.md"
