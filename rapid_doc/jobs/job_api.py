"""异步 Job 创建 API 的路由装配。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from .job_admission import JobAdmissionError, JobAdmissionService
from .job_config import JobSettings
from .job_store import IdempotencyConflictError, QueueCapacityError


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
        "callbackState": "pending" if job["callback_url"] else "not_requested",
        "cache": {
            "role": cache_role,
            "resultSource": _result_source(job_state, cache_role),
        },
        "submittedAt": _timestamp_as_iso(job["submitted_at"]),
        "statusUrl": f"/jobs/{job_id}",
        "resultUrl": f"/jobs/{job_id}/result",
        "cancelUrl": f"/jobs/{job_id}/cancel",
    }


def _result_source(job_state: str, cache_role: str) -> str | None:
    if job_state != "succeeded":
        return None
    return {"owner": "ocr", "follower": "shared_inflight", "hit": "cache"}[cache_role]


def _timestamp_as_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


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
