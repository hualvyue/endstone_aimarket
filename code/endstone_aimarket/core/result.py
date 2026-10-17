"""统一的 Result 返回类型。

约定：所有业务方法返回 Result，**不用异常表达业务失败**。
只有参数非法或存储损坏才抛 AimarketError。
这样调用方必须显式处理失败分支，杜绝「返回值没人检查、错误被静默吞掉」。
"""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from .errors import ErrorCode

T = TypeVar("T")


class Result(Generic[T]):
    __slots__ = ("ok", "code", "value", "message")

    def __init__(self, ok: bool, code: ErrorCode, value: Optional[T] = None, message: str = ""):
        self.ok = ok
        self.code = code
        self.value = value
        self.message = message

    # ---------- 构造 ----------

    @staticmethod
    def success(value: T = None) -> "Result[T]":
        return Result(True, ErrorCode.OK, value)

    @staticmethod
    def failure(code: ErrorCode, message: str = "") -> "Result[T]":
        return Result(False, code, None, message)

    # ---------- 消费 ----------

    def unwrap(self) -> T:
        if not self.ok:
            raise ValueError(f"unwrap 失败: {self.code.value} {self.message}")
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value if self.ok and self.value is not None else default

    def map(self, fn) -> "Result":
        if not self.ok:
            return Result(False, self.code, None, self.message)
        return Result.success(fn(self.value))

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        if self.ok:
            return f"<Result ok value={self.value!r}>"
        return f"<Result fail {self.code.value} {self.message!r}>"
