# RapidDoc AMD64 内网部署说明

本文档用于客户服务器离线部署 CPU 版本 RapidDoc，包含 API、Gradio、异步 Job、MySQL 任务表和本地文件挂载。

## 1. 准备物料

上传到客户服务器：

```text
rapid-doc-cpu-slim-amd64-v2.tar
.env
```

建议目录：

```bash
mkdir -p /opt/rapid-doc
mkdir -p /data/rapid-doc/jobs
```

示例放置：

```text
/opt/rapid-doc/rapid-doc-cpu-slim-amd64-v2.tar
/opt/rapid-doc/.env
/data/rapid-doc/jobs/
```

## 2. 加载镜像

```bash
cd /opt/rapid-doc
docker load -i rapid-doc-cpu-slim-amd64-v2.tar
docker images | grep rapid-doc
```

应看到类似镜像：

```text
rapid-doc   cpu-slim-amd64-v2
```

## 3. 创建 MySQL Database

RapidDoc 会自动建表，但 database 需要提前创建。

```sql
CREATE DATABASE IF NOT EXISTS rapid_doc
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

如果使用命令行：

```bash
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS rapid_doc CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

启动后会自动创建：

```text
jobs
parse_cache
callback_outbox
service_heartbeats
job_queue_sequence
schema_migrations
```

## 4. .env 示例

保存为：

```text
/opt/rapid-doc/.env
```

内容示例：

```bash
API_PORT=8888
GRADIO_SERVER_NAME=0.0.0.0
GRADIO_SERVER_PORT=7860
LOG_LEVEL=INFO
TZ=Asia/Shanghai

RAPID_DOC_ASYNC_ENABLED=true
RAPID_DOC_WORKER_PROCESSES=1
RAPID_DOC_JOB_DATA_DIR=/app/output/jobs

RAPID_DOC_DB_BACKEND=mysql
RAPID_DOC_MYSQL_HOST=127.0.0.1
RAPID_DOC_MYSQL_PORT=3306
RAPID_DOC_MYSQL_DATABASE=rapid_doc
RAPID_DOC_MYSQL_USER=root
RAPID_DOC_MYSQL_PASSWORD=请替换为实际密码
RAPID_DOC_MYSQL_POOL_SIZE=5

RAPID_DOC_MAX_FILE_SIZE_MB=100
RAPID_DOC_ALLOWED_EXTENSIONS=pdf,doc,docx,xls,xlsx,png,jpg,jpeg,tif,tiff
RAPID_DOC_MAX_PDF_PAGES=50

RAPID_DOC_QUEUE_EXPIRE_MINUTES=43200
RAPID_DOC_RESULT_TTL_MINUTES=10080
RAPID_DOC_CACHE_TTL_MINUTES=43200
RAPID_DOC_JOB_MAX_RUN_MINUTES=60
RAPID_DOC_JOB_LEASE_SECONDS=60
RAPID_DOC_JOB_HEARTBEAT_SECONDS=30
RAPID_DOC_MAINTENANCE_WATCHDOG_INTERVAL_SECONDS=10
RAPID_DOC_MAINTENANCE_SWEEPER_INTERVAL_SECONDS=60

RAPID_DOC_CALLBACK_CONNECT_TIMEOUT_SECONDS=5
RAPID_DOC_CALLBACK_READ_TIMEOUT_SECONDS=30

RAPID_DOC_COMPONENT_RESTART_DELAY_SECONDS=60
RAPID_DOC_LOG_DIR=/app/output/jobs/logs
RAPID_DOC_UNIFIED_STDOUT_LOGGING=true
RAPID_DOC_LOG_MAX_BYTES=104857600
RAPID_DOC_LOG_RETENTION_DAYS=15
```

说明：

- Linux 客户服务器如果 MySQL 在本机，推荐 `RAPID_DOC_MYSQL_HOST=127.0.0.1`。
- 如果 MySQL 是独立服务器，改成对应内网 IP。
- `RAPID_DOC_JOB_DATA_DIR` 不要改，容器内固定使用 `/app/output/jobs`。

## 5. 启动容器

生产 Linux 推荐使用 host 网络：

```bash
docker run -d \
  --name rapid-doc \
  --restart unless-stopped \
  --network host \
  --shm-size=512m \
  -v /data/rapid-doc/jobs:/app/output/jobs \
  -v /opt/rapid-doc/.env:/app/.env:ro \
  rapid-doc:cpu-slim-amd64-v2
```

如果客户现场不能使用 host 网络，可改为端口映射方式：

```bash
docker run -d \
  --name rapid-doc \
  --restart unless-stopped \
  --shm-size=512m \
  -p 8888:8888 \
  -p 7860:7860 \
  -v /data/rapid-doc/jobs:/app/output/jobs \
  -v /opt/rapid-doc/.env:/app/.env:ro \
  rapid-doc:cpu-slim-amd64-v2
```

端口映射方式下，如果 MySQL 在宿主机，`127.0.0.1` 可能不可用，需要改成宿主机内网 IP。

## 6. 验证

```bash
curl http://127.0.0.1:8888/health/live
curl http://127.0.0.1:8888/health/ready
```

返回 `200 OK` 后表示服务可用。

访问 Gradio：

```text
http://服务器IP:7860
```

API 地址：

```text
http://服务器IP:8888
```

## 7. 查看日志

统一日志目录：

```bash
ls -lh /data/rapid-doc/jobs/logs
tail -f /data/rapid-doc/jobs/logs/rapid-doc-$(date +%F).log
```

Docker 日志：

```bash
docker logs -f rapid-doc
```

## 8. 停止与重启

```bash
docker stop rapid-doc
docker start rapid-doc
docker restart rapid-doc
```

## 9. 数据挂载说明

宿主机目录：

```text
/data/rapid-doc/jobs
```

容器内目录：

```text
/app/output/jobs
```

该目录保存：

```text
inputs/      上传原文件
attempts/    OCR 临时结果
cache/       OCR 缓存结果
logs/        统一日志
control/     内部控制信号
```

