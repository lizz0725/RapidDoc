#!/usr/bin/env python3
"""Patch container-only defaults for the CPU-slim image."""
import os
from pathlib import Path


APP_ROOT = Path(os.getenv("RAPID_DOC_APP_ROOT", "/app"))
GRADIO_APP = APP_ROOT / "rapid_doc/cli/gradio_app.py"


OFFICE_VIEWER_ASSET_HELPER = '''def _load_office_viewer_asset(filename: str) -> str:
    asset_dir = Path(os.getenv("RAPID_DOC_OFFICE_VIEWER_ASSET_DIR", "/app/vendor/jit-viewer"))
    try:
        content = (asset_dir / filename).read_text(encoding="utf-8")
    except OSError:
        return ""
    if filename.endswith(".js"):
        content = content.replace("</script", "<" + "\\\\/script")
    return content


'''


PATCHES = {
    APP_ROOT / "app.py": [
        (
            "    formula_enable: bool = Form(True),",
            "    formula_enable: bool = Form(False),",
        ),
        (
            '    uvicorn.run(app, host="0.0.0.0", port=8888)',
            '    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("API_PORT", "8888")))',
        ),
    ],
    GRADIO_APP: [
        (
            "def launch_kwargs(**kwargs):\n"
            "    params = inspect.signature(gr.Blocks.launch).parameters\n"
            "    return {key: value for key, value in kwargs.items() if key in params}\n\n\n"
            "OFFICE_VIEWER_HEAD = \"\"\"\n"
            "<link rel=\"stylesheet\" href=\"https://unpkg.com/jit-viewer@1.1.0/dist/iife/jit-viewer.min.css\">\n"
            "<script src=\"https://unpkg.com/jit-viewer@1.1.0/dist/iife/jit-viewer.min.js\"></script>",
            "def launch_kwargs(**kwargs):\n"
            "    params = inspect.signature(gr.Blocks.launch).parameters\n"
            "    return {key: value for key, value in kwargs.items() if key in params}\n\n\n"
            f"{OFFICE_VIEWER_ASSET_HELPER}"
            "OFFICE_VIEWER_ASSETS_HEAD = (\n"
            "    \"<style>\" + _load_office_viewer_asset('jit-viewer.min.css') + \"</style>\\n\"\n"
            "    \"<script>\" + _load_office_viewer_asset('jit-viewer.min.js') + \"</script>\\n\"\n"
            ")\n\n\n"
            "OFFICE_VIEWER_HEAD = OFFICE_VIEWER_ASSETS_HEAD + \"\"\"",
        ),
        (
            "                        formula_enable = gr.Checkbox(label='Enable formula recognition', value=True)",
            "                        formula_enable = gr.Checkbox(label='Enable formula recognition', value=False)",
        ),
    ],
}


def patch_file(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(f"Expected text not found in {path}: {old}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    for path, replacements in PATCHES.items():
        patch_file(path, replacements)


if __name__ == "__main__":
    main()
