"""Fixed admission limits for the first single-host CPU deployment."""


class JobAdmissionLimits:
    MAX_QUEUED_JOBS = 100
    MAX_RETAINED_BYTES = 10 * 1024 * 1024 * 1024
