"""统一日志：logger 未就绪时降级到 print，避免 on_load 阶段丢日志。"""
from __future__ import annotations

import threading
from typing import Optional


class Log:
    def __init__(self, prefix: str = "Aimarket"):
        self.prefix = prefix
        self._logger = None
        self._lock = threading.Lock()
        self._buffer: list = []

    def bind(self, logger) -> None:
        """绑定 Endstone 的 logger，并把缓冲的日志补发。"""
        with self._lock:
            self._logger = logger
            buffered, self._buffer = self._buffer, []
        for level, text in buffered:
            self._emit(level, text)

    def _emit(self, level: str, text: str) -> None:
        if self._logger is None:
            print(f"[{self.prefix}/{level.upper()}] {text}")
            return
        fn = getattr(self._logger, level, None)
        try:
            if callable(fn):
                fn(text)
            else:
                self._logger.info(text)
        except Exception:
            print(f"[{self.prefix}/{level.upper()}] {text}")

    def log(self, level: str, text: str) -> None:
        text = str(text)
        with self._lock:
            ready = self._logger is not None
            if not ready:
                self._buffer.append((level, text))
        if ready:
            self._emit(level, text)

    def info(self, text: str) -> None:
        self.log("info", text)

    def warning(self, text: str) -> None:
        self.log("warning", text)

    def error(self, text: str) -> None:
        self.log("error", text)

    def debug(self, text: str) -> None:
        self.log("debug", text)
