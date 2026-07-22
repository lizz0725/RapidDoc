"""异步 Job 创建 API 的路由装配。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from .job_admission import JobAdmissionError, JobAdmissionService, normalize_tenant_id
from .job_config import JobSettings
from .job_store import IdempotencyConflictError, QueueCapacityError
from .job_types import JobState


def install_job_api(app: FastAPI) -> None:
    """把新的异步 Job 路由挂载到既有 FastAPI 应用。"""

    router = APIRouter(tags=["jobs"])

    @router.post("/jobs", status_code=202, summary="创建异步文档解析任务")
    async def create_job(
        request: Request,
        file: UploadFile = File(...),
        tenant_id: str | None = Form(None, alias="tenantId"),
        business_ref: str | None = Form(None, alias="businessRef"),
        callback_url: str | None = Form(None, alias="callbackUrl"),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        service = _job_service(request.app)
        if not service.settings.async_enabled:
            return _error_response(
                503,
                "ASYNC_JOBS_DISABLED",
                "当前部署未启用异步 Job API。",
            )
        try:
            creation = await service.create_job(
                upload=file,
                tenant_id=tenant_id,
                business_ref=business_ref,
                callback_url=callback_url,
                idempotency_key=idempotency_key,
            )
        except JobAdmissionError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        except IdempotencyConflictError:
            return _error_response(
                409,
                "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST",
                "同一租户的 Idempotency-Key 已用于不同请求。",
            )
        except QueueCapacityError:
            return _error_response(
                429,
                "QUEUE_CAPACITY_EXCEEDED",
                "OCR 队列已达到第一期固定容量。",
                headers={"Retry-After": "60"},
            )
        return JSONResponse(status_code=202, content=_creation_response(creation.job))

    @router.get("/jobs/{job_id}", summary="查询异步文档解析任务状态")
    def get_job_status(
        job_id: str,
        request: Request,
        tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    ) -> JSONResponse:
        service = _job_service(request.app)
        try:
            normalized_tenant_id = normalize_tenant_id(tenant_id)
        except JobAdmissionError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        service.initialize()
        job = service.store.get_job_status(normalized_tenant_id, job_id)
        if job is None:
            return _job_not_found_response()
        return JSONResponse(content=_status_response(job, service.settings.worker_processes))

    @router.get("/jobs/{job_id}/result", summary="获取异步文档解析结果")
    def get_job_result(
        job_id: str,
        request: Request,
        tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    ) -> JSONResponse:
        service = _job_service(request.app)
        try:
            normalized_tenant_id = normalize_tenant_id(tenant_id)
        except JobAdmissionError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        service.initialize()
        job = service.store.get_job(normalized_tenant_id, job_id)
        if job is None:
            return _job_not_found_response()
        job_state = job["job_state"]
        if job_state in {
            JobState.QUEUED.value,
            JobState.WAITING_FOR_RESULT.value,
            JobState.RUNNING.value,
            JobState.PUBLISHING.value,
        }:
            return JSONResponse(
                status_code=202,
                content={"jobId": job_id, "jobState": job_state, "result": None},
            )
        if job_state == JobState.RESULT_EXPIRED.value:
            return _error_response(410, "RESULT_EXPIRED", "任务结果已按保留期清理。")
        if job_state != JobState.SUCCEEDED.value:
            return _error_response(
                409,
                "JOB_RESULT_UNAVAILABLE",
                "当前任务状态无法获取识别结果。",
            )
        if not job["result_path"]:
            return _error_response(410, "RESULT_ARTIFACT_MISSING", "任务结果文件已不可用。")
        try:
            result = service.artifacts.read_result_json(job["result_path"])
        except FileNotFoundError:
            return _error_response(410, "RESULT_ARTIFACT_MISSING", "任务结果文件已不可用。")
        except (OSError, ValueError):
            return _error_response(500, "RESULT_ARTIFACT_INVALID", "任务结果文件无法读取。")
        return JSONResponse(content=_result_response(job, result))

    @router.post("/jobs/{job_id}/cancel", summary="取消尚未开始的异步任务")
    def cancel_job(
        job_id: str,
        request: Request,
        tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    ) -> JSONResponse:
        service = _job_service(request.app)
        try:
            normalized_tenant_id = normalize_tenant_id(tenant_id)
        except JobAdmissionError as exc:
            return _error_response(exc.status_code, exc.code, exc.message)
        service.initialize()
        cancellation = service.store.cancel_job(normalized_tenant_id, job_id)
        if cancellation.job is None:
            return _job_not_found_response()
        if not cancellation.cancelled:
            return _error_response(
                409,
                "JOB_CANNOT_BE_CANCELLED",
                "仅排队中或等待共享结果的任务可以取消。",
            )
        return JSONResponse(
            content={
                "jobId": job_id,
                "jobState": JobState.CANCELLED.value,
                "cancelledAt": _timestamp_as_iso(cancellation.job["finished_at"]),
            }
        )

    @router.get("/ops/jobs/queue", summary="查看真实 OCR 排队任务")
    def get_operations_queue(request: Request) -> JSONResponse:
        """仅面向受控内网运维网关，不以 tenantId 限制查询范围。"""

        service = _job_service(request.app)
        service.initialize()
        snapshot = service.store.get_queue_snapshot()
        items = snapshot["items"]
        return JSONResponse(
            content={
                "generatedAt": _timestamp_as_iso(int(datetime.now(timezone.utc).timestamp())),
                "queuedCount": len(items),
                "runningJobCount": snapshot["running_job_count"],
                "workerCapacity": service.settings.worker_processes,
                "items": [
                    {
                        "queuePosition": item["queue_position"],
                        "jobId": item["job_id"],
                        "tenantId": item["tenant_id"],
                        "sourceFilename": item["source_filename"],
                        "submittedAt": _timestamp_as_iso(item["submitted_at"]),
                    }
                    for item in items
                ],
            }
        )

    app.include_router(router)


def _job_service(app: FastAPI) -> JobAdmissionService:
    service = getattr(app.state, "rapid_doc_job_service", None)
    if service is None:
        service = JobAdmissionService(JobSettings.from_env())
        app.state.rapid_doc_job_service = service
    return service


def _creation_response(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job["job_id"]
    job_state = job["job_state"]
    cache_role = job["cache_role"]
    return {
        "jobId": job_id,
        "jobState": job_state,
        "callbackState": _callback_state(job),
        "cache": {
            "role": cache_role,
            "resultSource": _result_source(job_state, cache_role),
        },
        "submittedAt": _timestamp_as_iso(job["submitted_at"]),
        "statusUrl": f"/jobs/{job_id}",
        "resultUrl": f"/jobs/{job_id}/result",
        "cancelUrl": f"/jobs/{job_id}/cancel",
    }


def _status_response(job: dict[str, Any], worker_capacity: int) -> dict[str, Any]:
    job_id = job["job_id"]
    return {
        "jobId": job_id,
        "jobState": job["job_state"],
        "callbackState": _callback_state(job),
        "queueSeq": job["queue_seq"],
        "queuePosition": job["queue_position"],
        "aheadQueuedCount": job["ahead_queued_count"],
        "runningJobCount": job["running_job_count"],
        "workerCapacity": worker_capacity,
        "cache": {
            "role": job["cache_role"],
            "resultSource": _result_source(job["job_state"], job["cache_role"]),
        },
        "sourcePageCount": job["source_page_count"],
        "processedPageCount": job["processed_page_count"],
        "truncated": bool(job["truncated"]),
        "warnings": _warnings(job["warnings_json"]),
        "submittedAt": _timestamp_as_iso(job["submitted_at"]),
        "startedAt": _timestamp_as_iso(job["started_at"]),
        "finishedAt": _timestamp_as_iso(job["finished_at"]),
        "resultUrl": f"/jobs/{job_id}/result",
        "error": _error_details(job),
    }


def _result_response(job: dict[str, Any], artifact: dict[str, object]) -> dict[str, Any]:
    artifact_metadata = artifact.get("metadata")
    metadata = dict(artifact_metadata) if isinstance(artifact_metadata, dict) else {}
    metadata.update(
        {
            "sourcePageCount": job["source_page_count"],
            "processedPageCount": job["processed_page_count"],
            "truncated": bool(job["truncated"]),
            "warnings": _warnings(job["warnings_json"]),
            "resultSource": _result_source(job["job_state"], job["cache_role"]),
        }
    )
    return {
        "jobId": job["job_id"],
        "jobState": job["job_state"],
        "result": {"markdown": artifact["markdown"], "metadata": metadata},
    }


def _result_source(job_state: str, cache_role: str) -> str | None:
    if job_state != "succeeded":
        return None
    return {"owner": "ocr", "follower": "shared_inflight", "hit": "cache"}[cache_role]


def _callback_state(job: dict[str, Any]) -> str:
    return job.get("callback_state") or ("pending" if job["callback_url"] else "not_requested")


def _warnings(serialized_warnings: str | None) -> list[dict[str, Any]]:
    if not serialized_warnings:
        return []
    try:
        warnings = json.loads(serialized_warnings)
    except json.JSONDecodeError:
        return []
    return warnings if isinstance(warnings, list) else []


def _error_details(job: dict[str, Any]) -> dict[str, str] | None:
    if not job["error_code"]:
        return None
    return {"code": job["error_code"], "message": job["error_message"] or ""}


def _timestamp_as_iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _job_not_found_response() -> JSONResponse:
    return _error_response(404, "JOB_NOT_FOUND", "未找到对应租户下的任务。")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )
