FROM python:3.10.16-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update && \
    apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-noto-core \
        fonts-noto-cjk \
        fontconfig \
        curl \
        ca-certificates \
        libgl1 && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_DEFAULT_TIMEOUT=300
ENV PIP_RETRIES=10

COPY pyproject.toml README.md /app/
COPY rapid_doc /app/rapid_doc

RUN python3 -m pip install --upgrade pip setuptools wheel --break-system-packages && \
    python3 -m pip install --no-cache-dir --prefer-binary --break-system-packages \
        '.[cpu,api,gradio]' && \
    python3 -m pip cache purge

ENV PYTHONPATH=/app
ENV API_PORT=8888
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860
ENV LOG_LEVEL=INFO
ENV MINERU_DEVICE_MODE=cpu
ENV RAPID_MODELS_DIR=/app/models
ENV RAPID_DOC_OFFICE_VIEWER_ASSET_DIR=/app/vendor/jit-viewer

COPY docker/download_file.py docker/models_download_utils.py docker/download_models_cpu_slim.py /app/

RUN python3 download_models_cpu_slim.py

COPY docker/.env.example /app/
COPY docker/app.py docker/file_converter.py /app/
COPY docker/patch_cpu_slim_defaults.py docker/start_api_gradio_cpu_slim.sh /app/
COPY docker/vendor /app/vendor

RUN sed -i 's/\r$//' /app/start_api_gradio_cpu_slim.sh && \
    chmod +x /app/start_api_gradio_cpu_slim.sh && \
    python3 /app/patch_cpu_slim_defaults.py

EXPOSE 8888 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:${API_PORT}/health || exit 1

CMD ["./start_api_gradio_cpu_slim.sh"]
