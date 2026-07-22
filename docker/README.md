# RapidDoc 镜像部署指南

镜像已推送至 [Docker Hub](https://hub.docker.com/r/hzkitty/rapid-doc)

## 镜像构建

如果需要自己构建镜像

### 执行构建命令

```bash
cd docker

# 1. CPU 模式
docker build -f Dockerfile -t hzkitty/rapid-doc:0.9.9 .

# 2. GPU 模式
docker build -f DockerfileGPU -t hzkitty/rapid-doc:0.9.9-gpu .
```


## 运行部署

### 1. CPU 模式

仅CPU推理，资源占用较少：
```bash
docker-compose -f docker-compose.yml up -d
```
### 2. GPU 模式
```bash
docker-compose -f docker-compose-gpu.yml up -d
```

## CPU slim 异步 Job 镜像

`cpu-slim.Dockerfile` 用于单机、纯 CPU、离线私有化部署。镜像内包含 OCR 所需模型，运行阶段无需下载模型；它会同时启动 FastAPI、Gradio、OCR Worker、维护进程和回调分发器。

在仓库根目录构建：

```bash
docker build -f docker/cpu-slim.Dockerfile -t rapid-doc:cpu-slim .
```

本机 Apple Silicon 构建供 AMD64 服务器使用的镜像：

```bash
docker buildx build \
  --platform linux/amd64 \
  -f docker/cpu-slim.Dockerfile \
  -t rapid-doc:cpu-slim-amd64 \
  --load .

docker save -o rapid-doc-cpu-slim-amd64.tar rapid-doc:cpu-slim-amd64
```

### 推荐挂载方式

模型保留在镜像的 `/app/models`，无需挂载。将配置、定制 release 和 Job 数据分别放在宿主机：

```text
/opt/rapid-doc/config/.env
/opt/rapid-doc/release/
/data/rapid-doc/jobs/
```

```bash
docker run -d \
  --name rapid-doc \
  --restart unless-stopped \
  -p 8888:8888 \
  -p 7860:7860 \
  -v /opt/rapid-doc/config/.env:/app/.env:ro \
  -v /opt/rapid-doc/release:/opt/rapid-doc/release:ro \
  -v /data/rapid-doc/jobs:/app/output/jobs \
  -e PYTHONPATH=/opt/rapid-doc/release:/app \
  --entrypoint /bin/bash \
  rapid-doc:cpu-slim-amd64 \
  /opt/rapid-doc/release/start_api_gradio_cpu_slim.sh
```

release 目录内可覆盖 `app.py`、自定义模块、SQL 和启动脚本；由 `/bin/bash` 显式执行挂载脚本，因此不会依赖宿主机是否保留可执行位。不要挂载 `/app`、`/app/rapid_doc` 或 `/app/models`，避免遮蔽镜像内的依赖、原始代码或模型。只更新 release 或 `.env` 后，执行 `docker restart rapid-doc` 即可生效；修改 Python/系统依赖、模型或基础镜像时需要重新构建镜像。

### 关键 Job 配置

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `RAPID_DOC_ASYNC_ENABLED` | `true` | 是否启用新增的 `/jobs` API 和后台进程。 |
| `RAPID_DOC_WORKER_PROCESSES` | `1` | OCR OS 进程数，不等同于 Uvicorn worker 数。 |
| `RAPID_DOC_JOB_DATA_DIR` | `/app/output/jobs` | SQLite、原文件、临时结果与缓存目录。 |
| `RAPID_DOC_MAX_FILE_SIZE_MB` | `100` | 单个上传文件大小上限，单位 MB。 |
| `RAPID_DOC_ALLOWED_EXTENSIONS` | `pdf,doc,docx,xls,xlsx,png,jpg,jpeg,tif,tiff` | Job 上传白名单。 |
| `RAPID_DOC_MAX_PDF_PAGES` | `100` | PDF 最多处理前 N 页。 |
| `RAPID_DOC_QUEUE_EXPIRE_MINUTES` | `43200` | 任务最长排队时长。 |
| `RAPID_DOC_RESULT_TTL_MINUTES` | `10080` | 成功 Job 的结果查询保留时长。 |
| `RAPID_DOC_CACHE_TTL_MINUTES` | `43200` | 成功结果缓存保留时长。 |
| `RAPID_DOC_JOB_MAX_RUN_MINUTES` | `60` | 单个 OCR Job 最大执行时长。 |

健康检查：

```bash
curl http://localhost:8888/health/live
curl http://localhost:8888/health/ready
```

容器刚启动时，`/health/ready` 可能短暂返回 `503`，直到 OCR Worker、维护进程和回调分发器都写入心跳。返回 `200` 后才应接入流量。已构建完成的镜像可在无外网环境中运行；若业务方提交了 `callbackUrl`，该回调地址仍需要在部署网络中可达。

## 服务端口

- **8888**: RapidDoc Web API 服务端口

## API 使用

### 健康检查

```bash
curl http://localhost:8888/health
```

### 文档解析 API

```bash
# 上传文档进行解析
curl -X POST "http://localhost:8888/parse" \
     -F "file=@document.pdf" \
     -F "mode=pipeline"
```

## 配置文件详解

### .env 环境变量配置文件

`.env` 文件用于配置服务器和系统运行参数，支持以下配置项：

#### 基础配置

| 变量名                                | 默认值      | 说明                   |
|------------------------------------|----------|----------------------|
| `API_PORT`                         | `8888`   | RapidDoc Web API 端口  |
| `PADDLEOCRVL_VERSION`              |          | paddleocr-vl 版本      |
| `PADDLEOCRVL_VL_REC_BACKEND`       |          | paddleocr-vl backend |
| `PADDLEOCRVL_VL_VL_REC_SERVER_URL` |          | paddleocr-vl url     |


### 系统配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `STARTUP_WAIT_TIME` | `15` | 启动等待时间（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `RAPID_MODELS_DIR` | `/app/models` | 模型文件存储目录 |
