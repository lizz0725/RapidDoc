"""第一期单机 CPU 部署固定的任务准入限制。"""


class JobAdmissionLimits:
    MAX_QUEUED_JOBS = 100
    MAX_RETAINED_BYTES = 10 * 1024 * 1024 * 1024
