# RapidDoc 异步文档解析 Job API

**版本**: v1
**基准路径**: `{RapidDoc 部署地址}`
**协议**: HTTP/1.1, JSON

---

## 1. 概述

异步 Job API 提供文档解析任务的创建、状态查询、结果获取与取消能力。文档经过 OCR 解析后返回 Markdown 格式结果。

### 1.1 端到端流程

```
POST /jobs          → 202 { "jobId": "..." }
  │
  ├─> GET /jobs/{jobId}  (轮询状态)
  │     queued → running → publishing → succeeded
  │
  ├─> GET /jobs/{jobId}/result  (succeeded 后获取结果)
  │
  └─> POST /jobs/{jobId}/cancel  (仅 queued/waiting_for_result 可取消)
```

### 1.2 Job 状态机

| 状态 | 终态 | 含义 |
|------|:--:|------|
| `queued` | | 已在 OCR 队列中等待 |
| `waiting_for_result` | | 等待同一文件的其他 owner 解析完成（缓存 follower） |
| `running` | | OCR Worker 正在解析 |
| `publishing` | | 解析完成，正在发布结果 |
| `succeeded` | ✅ | 解析成功，结果可获取 |
| `failed` | ✅ | 解析失败 |
| `cancelled` | ✅ | 已被取消 |
| `expired` | ✅ | 排队超时（默认 30 天） |
| `result_expired` | ✅ | 结果已按 TTL 清理（默认 7 天） |

### 1.3 缓存角色

同一租户下，如果提交内容（SHA256）相同的文件：

| role | 说明 |
|------|------|
| `owner` | 第一个提交，进入 OCR 队列 |
| `follower` | 后续提交，挂载等待 owner 结果 |
| `hit` | 缓存命中，结果立即可用（绕过 OCR） |

### 1.4 通用错误格式

所有 HTTP 错误（≥400）响应体：

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "人类可读的错误描述"
  }
}
```

---

## 2. 接口详情

### 2.1 创建解析任务

```
POST /jobs
Content-Type: multipart/form-data
```

#### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:--:|------|
| Body (file) | `file` | binary | ✅ | 待解析文件，最大 100 MB |
| Body (form) | `tenantId` | string | ✅ | 租户 ID，1-128 可打印字符 |
| Body (form) | `businessRef` | string | | 业务引用，1-128 可打印字符 |
| Body (form) | `callbackUrl` | string | | 终态回调 URL（HTTP/HTTPS，≤2048 字符） |
| Header | `Idempotency-Key` | string | | 幂等键，同一租户下相同值返回已有结果，≤256 字符 |

#### 支持的源文件格式

`.pdf` `.doc` `.docx` `.xls` `.xlsx` `.png` `.jpg` `.jpeg` `.tif` `.tiff`

文件以魔术字节检测实际类型，不信任扩展名和 MIME 类型。

#### 成功响应 (202)

```json
{
  "jobId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "jobState": "queued",
  "callbackState": "not_requested",
  "cache": {
    "role": "owner",
    "resultSource": null
  },
  "submittedAt": "2025-01-15T10:30:00Z",
  "statusUrl": "/jobs/01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "resultUrl": "/jobs/01JQABCDEFGHJKMNPQRSTVWXYZ0/result",
  "cancelUrl": "/jobs/01JQABCDEFGHJKMNPQRSTVWXYZ0/cancel"
}
```

| 字段 | 说明 |
|------|------|
| `jobId` | ULID 格式的 26 位任务 ID |
| `jobState` | `queued` / `waiting_for_result` / `succeeded`（缓存命中立即可用） |
| `callbackState` | `not_requested`（未配回调） / `pending`（待投递） |
| `cache.role` | `owner` / `follower` / `hit` |
| `cache.resultSource` | 仅 succeeded 时有值：`ocr` / `shared_inflight` / `cache` |
| `submittedAt` | 提交时间 ISO-8601 |
| `statusUrl` | 状态查询的相对路径 |
| `resultUrl` | 结果获取的相对路径 |
| `cancelUrl` | 取消任务的相对路径 |

#### 错误码

| HTTP | code | 触发条件 |
|------|------|----------|
| 413 | `FILE_TOO_LARGE` | 文件超过 `RAPID_DOC_MAX_FILE_SIZE_MB`（默认 100 MB） |
| 415 | `UNSUPPORTED_FILE_TYPE` | 扩展名不在白名单 / 魔术字节不匹配 / MIME 不一致 |
| 422 | `INVALID_TENANT_ID` | tenantId 为空或超过 128 字符 |
| 422 | `INVALID_BUSINESS_REF` | businessRef 超过 128 字符 |
| 422 | `INVALID_CALLBACK_URL` | callbackUrl 格式非法（非 http/https 或无主机名） |
| 422 | `INVALID_IDEMPOTENCY_KEY` | Idempotency-Key 超过 256 字符 |
| 422 | `INVALID_PDF` | PDF 文件无法读取页数 |
| 409 | `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST` | 幂等键相同但请求内容不同 |
| 429 | `QUEUE_CAPACITY_EXCEEDED` | OCR 队列满（100 个） |
| 429 | `STORAGE_CAPACITY_EXCEEDED` | 任务磁盘预算耗尽（10 GB） |
| 503 | `ASYNC_JOBS_DISABLED` | 部署未启用异步 Job API |

---

### 2.2 查询任务状态

```
GET /jobs/{jobId}
```

#### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:--:|------|
| Path | `jobId` | string | ✅ | 创建时返回的 ULID |
| Header | `X-Tenant-Id` | string | ✅ | 租户 ID |

#### 成功响应 (200)

```json
{
  "jobId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "jobState": "running",
  "callbackState": "pending",
  "queueSeq": 5,
  "queuePosition": null,
  "aheadQueuedCount": 0,
  "runningJobCount": 2,
  "workerCapacity": 4,
  "cache": {
    "role": "owner",
    "resultSource": null
  },
  "sourcePageCount": 12,
  "processedPageCount": 12,
  "truncated": false,
  "warnings": [],
  "submittedAt": "2025-01-15T10:30:00Z",
  "startedAt": "2025-01-15T10:31:05Z",
  "finishedAt": null,
  "queueDurationSeconds": 65,
  "runDurationSeconds": null,
  "totalDurationSeconds": 65,
  "resultUrl": "/jobs/01JQABCDEFGHJKMNPQRSTVWXYZ0/result",
  "error": null
}
```

| 字段 | 说明 |
|------|------|
| `queueSeq` | FIFO 序号（仅 owner 有值） |
| `queuePosition` | 当前排队位置（仅 `queued` 时有值，1-based） |
| `aheadQueuedCount` | 排在当前任务前的 owner 数 |
| `runningJobCount` | 当前正在执行 OCR 的 owner 数 |
| `workerCapacity` | Worker 进程总数 |
| `sourcePageCount` | 源文件总页数（仅 PDF） |
| `processedPageCount` | 实际处理页数（受 `maxPdfPages` 限制） |
| `truncated` | 是否因超限被截断 |
| `warnings` | 警告列表（如超页截断） |
| `queueDurationSeconds` | 排队耗时（秒） |
| `runDurationSeconds` | 执行耗时（秒），运行中为 null |
| `totalDurationSeconds` | 总耗时（秒） |
| `error` | 失败时返回 `{"code":"...","message":"..."}` |

#### 错误码

| HTTP | code | 说明 |
|------|------|------|
| 404 | `JOB_NOT_FOUND` | 指定租户下不存在该 jobId |
| 422 | `INVALID_TENANT_ID` | X-Tenant-Id 无效 |

---

### 2.3 获取解析结果

```
GET /jobs/{jobId}/result
```

#### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:--:|------|
| Path | `jobId` | string | ✅ | |
| Header | `X-Tenant-Id` | string | ✅ | |

#### 成功响应 (200)

仅在 `jobState: succeeded` 时返回。

```json
{
  "jobId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "jobState": "succeeded",
  "result": {
    "markdown": "# 文档标题\n\n解析内容...",
    "metadata": {
      "sourcePageCount": 12,
      "processedPageCount": 12,
      "truncated": false,
      "warnings": [],
      "resultSource": "ocr"
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `result.markdown` | Markdown 格式的文档解析结果 |
| `result.metadata.resultSource` | `ocr`（自行解析） / `shared_inflight`（共享结果） / `cache`（缓存命中） |

#### 非终态响应 (202)

任务仍在处理中，`result` 为 `null`：

```json
{
  "jobId": "...",
  "jobState": "queued",
  "result": null
}
```

#### 错误码

| HTTP | code | 说明 |
|------|------|------|
| 202 | — | 任务未完成，result 为 null |
| 404 | `JOB_NOT_FOUND` | 任务不存在 |
| 409 | `JOB_RESULT_UNAVAILABLE` | 状态不是 succeeded（如 failed/cancelled） |
| 410 | `RESULT_EXPIRED` | 结果已过期清理（默认 7 天） |
| 410 | `RESULT_ARTIFACT_MISSING` | 结果文件已不可用 |
| 500 | `RESULT_ARTIFACT_INVALID` | 结果文件无法读取 |

---

### 2.4 取消任务

```
POST /jobs/{jobId}/cancel
```

#### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:--:|------|
| Path | `jobId` | string | ✅ | |
| Header | `X-Tenant-Id` | string | ✅ | |

#### 限制

仅 `queued` 和 `waiting_for_result` 状态的任务可取消。取消 owner 时，第一个 follower 会自动提升为新 owner。

#### 成功响应 (200)

```json
{
  "jobId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "jobState": "cancelled",
  "cancelledAt": "2025-01-15T11:00:00Z"
}
```

#### 错误码

| HTTP | code | 说明 |
|------|------|------|
| 404 | `JOB_NOT_FOUND` | 任务不存在 |
| 409 | `JOB_CANNOT_BE_CANCELLED` | 状态不支持取消（已开始或已结束） |

---

### 2.5 运维：查看 OCR 队列 (内部接口)

```
GET /ops/jobs/queue
```

无需认证，返回当前 OCR FIFO 队列的快照（仅 owner，不含 follower 和终态任务）。

**响应 (200)**:

```json
{
  "generatedAt": "2025-01-15T10:30:00Z",
  "queuedCount": 5,
  "runningJobCount": 2,
  "workerCapacity": 4,
  "items": [
    {
      "queuePosition": 1,
      "jobId": "01JQ...",
      "tenantId": "tenant-001",
      "sourceFilename": "report.pdf",
      "submittedAt": "2025-01-15T10:25:00Z"
    }
  ]
}
```

---

## 3. 回调通知

### 3.1 触发条件

Job 到达终态时（`succeeded` / `failed` / `cancelled` / `expired`），若创建时提供了 `callbackUrl`，RapidDoc 会向其发送一次 HTTP POST。

### 3.2 回调请求

```
POST {callbackUrl}
Content-Type: application/json
X-RapidDoc-Delivery-Id: 01JQ...
X-RapidDoc-Timestamp: 2025-01-15T11:00:00Z
X-RapidDoc-Signature: v1={hmac_sha256_hex}  (仅配置签名密钥时存在)
```

**请求体**:

```json
{
  "deliveryId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "eventType": "job.terminal",
  "jobId": "01JQABCDEFGHJKMNPQRSTVWXYZ0",
  "jobState": "succeeded",
  "submittedAt": "2025-01-15T10:30:00Z",
  "startedAt": "2025-01-15T10:31:05Z",
  "finishedAt": "2025-01-15T10:32:00Z",
  "resultUrl": "/jobs/01JQABCDEFGHJKMNPQRSTVWXYZ0/result",
  "error": null
}
```

| 字段 | 说明 |
|------|------|
| `deliveryId` | 投递唯一 ID，可用于去重 |
| `eventType` | 固定值 `"job.terminal"` |
| `jobState` | 终态值 |
| `error` | 失败时有 `{"code":"...","message":"..."}`，成功时为 `null` |

### 3.3 签名验证

当 RapidDoc 部署配置了 `RAPID_DOC_CALLBACK_SIGNING_SECRET` 时，回调头包含 `X-RapidDoc-Signature`。验证算法：

```
HMAC-SHA256(RAPID_DOC_CALLBACK_SIGNING_SECRET, request_body_bytes) → hex digest
```

比对格式为 `v1={hex_digest}`。

### 3.4 投递语义

- **at-most-once**: 每条终态最多投递一次
- 期望 HTTP 2xx 即标记投递成功
- 非 2xx 或网络错误标记失败，不重试
- 可通过轮询 `GET /jobs/{jobId}` 的状态确认弥补投递失败场景

---

## 4. 集成建议

### 4.1 轮询策略

创建任务后，建议用**指数退避**轮询 `GET /jobs/{jobId}` 直至终态：

1. 初始间隔 2-3 秒
2. 每次加倍，上限 30 秒
3. 终态后调用 `GET /jobs/{jobId}/result` 获取结果

### 4.2 幂等性

同一租户下对相同文件+相同 callbackUrl 使用相同 `Idempotency-Key`，将返回已有结果而不重新排队。建议生成规则：

```
Idempotency-Key = sha256(tenantId + sourceSha256 + callbackUrl)
```

### 4.3 超时与异常

- 队列最长生存: 30 天 (`RAPID_DOC_QUEUE_EXPIRE_MINUTES`)
- 结果保留: 7 天 (`RAPID_DOC_RESULT_TTL_MINUTES`)
- 单任务最长解析时间: 60 分钟 (`RAPID_DOC_JOB_MAX_RUN_MINUTES`)

### 4.4 最终一致推荐流程

```
1. POST /jobs (with Idempotency-Key)
2. 保存返回的 jobId
3. 若 jobState == succeeded: 直接调用 /result
4. 否则: 轮询 GET /jobs/{jobId}
5. succeeded 后调用 GET /jobs/{jobId}/result
6. 若被 410 过期: 重新创建任务（相同幂等键返回缓存）
```

---

## 5. 容量限制

| 维度 | 默认值 | 环境变量 |
|------|--------|----------|
| OCR 队列长度 | 100 | 硬编码 |
| 磁盘预算 | 10 GB | 硬编码 |
| 单文件大小 | 100 MB | `RAPID_DOC_MAX_FILE_SIZE_MB` |
| PDF 最大页数 | 100 页 | `RAPID_DOC_MAX_PDF_PAGES` |
| Worker 并发数 | 1 | `RAPID_DOC_WORKER_PROCESSES` |
| 解析重试次数 | 2 | `RAPID_DOC_JOB_MAX_PROCESSING_ATTEMPTS` |
