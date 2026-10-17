"""配置管理：JSON 格式，零第三方依赖。

设计要点：
- 全插件只有这一份配置格式，支持嵌套段落与默认值，不引入 toml/yaml 依赖。
- 缺失键自动用默认值补全并写回，服主升级后不会因缺键而静默失效。
- 支持 `a.b.c` 路径取值与热重载。
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Optional


def _deep_merge(base: dict, override: dict) -> dict:
    """以 base 为准补全 override 缺失的键，override 已有的值优先。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, file: Path, defaults: dict, log=None):
        self.file = Path(file)
        self.defaults = copy.deepcopy(defaults)
        self.log = log
        self._lock = threading.RLock()
        self.data: dict = {}
        self.load()

    # ---------- 生命周期 ----------

    def load(self) -> None:
        with self._lock:
            existing = {}
            if self.file.exists():
                try:
                    with self.file.open("r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        existing = loaded
                except Exception as e:
                    if self.log:
                        self.log.error(f"配置文件 {self.file} 解析失败，使用默认值: {e}")
                    # 损坏文件不覆盖，留待服主排查
                    try:
                        backup = self.file.with_suffix(self.file.suffix + ".broken")
                        self.file.replace(backup)
                    except Exception:
                        pass
            self.data = _deep_merge(self.defaults, existing)
            self._save()

    def reload(self) -> None:
        self.load()

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self.log:
                self.log.error(f"写入配置 {self.file} 失败: {e}")

    # ---------- 读写 ----------

    def get(self, path: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self.data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def set(self, path: str, value: Any, *, save: bool = True) -> None:
        with self._lock:
            parts = path.split(".")
            node = self.data
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value
            if save:
                self._save()

    def section(self, path: str) -> dict:
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    def as_bool(self, path: str, default: bool = False) -> bool:
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")

    def as_int(self, path: str, default: int = 0) -> int:
        try:
            return int(self.get(path, default))
        except (TypeError, ValueError):
            return default

    def as_float(self, path: str, default: float = 0.0) -> float:
        """仅限非金额类参数（如概率），金额请走 money 模块的整数。"""
        try:
            return float(self.get(path, default))
        except (TypeError, ValueError):
            return default

    def raw(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)


def load_json_resource(target: Path, default_data: dict, log=None) -> Optional[dict]:
    """把一个默认资源文件（如价格表）落到目标路径并返回内容。"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open("w", encoding="utf-8") as f:
                json.dump(default_data, f, ensure_ascii=False, indent=2)
            return copy.deepcopy(default_data)
        with target.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        if log:
            log.error(f"加载资源 {target} 失败: {e}")
        return None
