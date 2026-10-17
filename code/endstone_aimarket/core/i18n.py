"""语言/文案管理。

核心约定：**文案缺失永远不能让界面变空白、也不能让业务中断**。

这里的策略：
1. 缺键回退到内置文案；
2. 内置也没有，就返回 key 本身（并在日志提示一次），**绝不返回空串**；
3. 语言文件缺失/损坏时自动重建，不影响启动。
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# 兜底：连资源文件都读不到时的最小可用文案
MINIMAL: Dict[str, str] = {
    "err.internal": "§c内部错误，请联系管理员",
    "err.storage_error": "§c数据存储异常，操作已取消",
    "err.not_player": "§c该命令只能在游戏内使用",
    "err.permission_denied": "§c你没有权限执行此操作",
    "err.invalid_amount": "§c金额无效",
    "err.insufficient_funds": "§c余额不足",
    "err.account_not_found": "§c找不到该账户",
}


class I18n:
    def __init__(self, lang_dir: Path, default_code: str = "zh_CN", log=None):
        self.lang_dir = Path(lang_dir)
        self.default_code = default_code
        self.log = log
        self._lock = threading.RLock()
        self._builtin: Dict[str, str] = {}
        self._override: Dict[str, str] = {}
        self._missing: set = set()
        self.load()

    # ---------- 加载 ----------

    def load(self) -> None:
        with self._lock:
            self._builtin = self._read_pack_resource()
            if not self._builtin:
                self._builtin = copy.deepcopy(MINIMAL)
                if self.log:
                    self.log.warning("未找到内置语言资源，仅使用最小兜底文案")
            self._override = self._read_user_file()

    def _read_pack_resource(self) -> Dict[str, str]:
        """读取随包分发的 resources/lang/<code>.json。"""
        try:
            path = Path(__file__).resolve().parent.parent / "resources" / "lang" / f"{self.default_code}.json"
            if not path.exists():
                return {}
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
        except Exception as e:
            if self.log:
                self.log.error(f"读取内置语言资源失败: {e}")
            return {}

    def _read_user_file(self) -> Dict[str, str]:
        path = self.lang_dir / f"{self.default_code}.json"
        try:
            if not path.exists():
                self._write_user_file({})
                return {}
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("语言文件格式不是对象")
            return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
        except Exception as e:
            if self.log:
                self.log.error(f"语言文件 {path} 损坏，已备份并重建: {e}")
            try:
                path.replace(path.with_suffix(".json.broken"))
            except Exception:
                pass
            self._write_user_file({})
            return {}

    def _write_user_file(self, data: Dict[str, str]) -> None:
        try:
            self.lang_dir.mkdir(parents=True, exist_ok=True)
            path = self.lang_dir / f"{self.default_code}.json"
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception as e:
            if self.log:
                self.log.error(f"写入语言文件失败: {e}")

    def reload(self) -> None:
        self.load()

    # ---------- 取文案 ----------

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._override or key in self._builtin

    def t(self, key: str, **kwargs: Any) -> str:
        """取文案。缺失时返回 key 本身，永远不返回空串。"""
        with self._lock:
            text = self._override.get(key)
            if text is None or text == "":
                text = self._builtin.get(key)
            if text is None or text == "":
                if key not in self._missing:
                    self._missing.add(key)
                    if self.log:
                        self.log.warning(f"缺少文案键 {key}，已原样显示（可在 lang/{self.default_code}.json 补充）")
                return key
            if kwargs:
                try:
                    return text.format(**kwargs)
                except Exception:
                    # 占位符不匹配时宁可原样返回，也不要抛异常打断交易流程
                    return text
            return text
