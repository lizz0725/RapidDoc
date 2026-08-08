#!/usr/bin/env python3
"""
Download the CPU-slim model set used by the Docker proof of concept.

This intentionally keeps the upstream Docker download flow intact and only
narrows the file-name allowlist used by download_file.DownloadFile.run().
"""
import os
import sys

import download_file
from models_download_utils import download_pipeline_models


CPU_SLIM_MODEL_ALLOWLIST = [
    # Layout: default pipeline layout model.
    "pp_doclayoutv3.onnx",
    # OCR orientation/font helpers. Main OCR v6 ONNX files are packaged under
    # rapid_doc/resources and copied into the image with the source tree.
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "FZYTK.TTF",
    # Table: default UNET + SLANetPlus CPU table path.
    "paddle_cls.onnx",
    "q_cls.onnx",
    "unet.onnx",
    "slanet-plus.onnx",
]


def main() -> int:
    os.environ.setdefault("MINERU_DEVICE_MODE", "cpu")
    download_file.CPU_MODEL = CPU_SLIM_MODEL_ALLOWLIST
    success = download_pipeline_models()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
