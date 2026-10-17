"""ID 生成：UUIDv7（时间有序，利于索引局部性与按时间排序扫描）。"""
from __future__ import annotations

import hashlib
import os
import time
import uuid


def new_uuid7() -> str:
    """生成 UUIDv7 字符串。

    布局：48bit 毫秒时间戳 | 4bit 版本(7) | 12bit 随机 | 2bit 变体 | 62bit 随机
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (ms << 80) | (7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


def short_id(n: int = 12) -> str:
    """短随机 ID，用于表单一次性 nonce 等。"""
    return uuid.uuid4().hex[:n]


def idempotency_key(*parts) -> str:
    """把若干业务要素压成稳定的幂等键。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize_name(name: str) -> str:
    """玩家名归一化，用于离线检索（大小写不敏感）。"""
    return (name or "").strip().lower()
