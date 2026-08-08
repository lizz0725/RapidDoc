"""异步 Job 子系统共用的状态类型。"""

from __future__ import annotations

import secrets
import time
from enum import Enum


class TextEnum(str, Enum):
    """可直接保存到 MySQL 和 JSON 的字符串枚举。"""

    def __str__(self) -> str:
        return self.value


class JobState(TextEnum):
    QUEUED = "queued"
    WAITING_FOR_RESULT = "waiting_for_result"
    RUNNING = "running"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    RESULT_EXPIRED = "result_expired"


class CacheRole(TextEnum):
    OWNER = "owner"
    FOLLOWER = "follower"
    HIT = "hit"


class CacheState(TextEnum):
    PROCESSING = "processing"
    READY = "ready"


class CallbackState(TextEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    FAILED = "failed"


_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: int | None = None) -> str:
    """生成 26 位、全大写且不含特殊符号的 ULID。"""

    timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("timestamp_ms must fit in 48 bits")

    value = int.from_bytes(
        timestamp_ms.to_bytes(6, "big") + secrets.token_bytes(10), "big"
    )
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD_BASE32[value & 31]
        value >>= 5
    return "".join(encoded)
