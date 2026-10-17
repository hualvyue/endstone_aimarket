"""显示项注册表。

玩家在 GUI 里勾选的只是**显示项的名字**，具体内容由这里注册的 provider 实时取。
加一个新指标 = 注册一个 provider，界面与存储都不用改。

显示项 key 的两种形态：
    * 内置项：`balance`、`debt`、`company_nav` …（见 BUILTIN）
    * 动态项：`stock:青云矿业`、`shop:我的钻石店` —— 冒号后面是对象名
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# provider 签名：provider(ctx) -> Optional[str]，返回 None 表示该项当前无内容、不显示
Provider = Callable[[Any], Optional[str]]


@dataclass
class DisplayItem:
    key: str
    name: str
    description: str = ""
    default_on: bool = False
    sort_order: int = 100
    prefix: str = ""      # 动态项的前缀，如 "stock:"


class DisplayRegistry:
    def __init__(self):
        self._items: Dict[str, DisplayItem] = {}
        self._providers: Dict[str, Provider] = {}
        self._prefix_providers: Dict[str, Provider] = {}

    # ==================== 注册 ====================

    def register(
        self,
        key: str,
        provider: Provider,
        *,
        name: str = "",
        description: str = "",
        default_on: bool = False,
        sort_order: int = 100,
    ) -> None:
        self._items[key] = DisplayItem(
            key=key,
            name=name or key,
            description=description,
            default_on=default_on,
            sort_order=sort_order,
        )
        self._providers[key] = provider

    def register_prefix(
        self,
        prefix: str,
        provider: Provider,
        *,
        name: str = "",
        description: str = "",
        sort_order: int = 500,
    ) -> None:
        """注册一类动态显示项，如 `stock:` —— 后面接公司名。"""
        normalized = prefix if prefix.endswith(":") else prefix + ":"
        self._prefix_providers[normalized] = provider
        self._items[normalized] = DisplayItem(
            key=normalized,
            name=name or prefix,
            description=description,
            sort_order=sort_order,
            prefix=normalized,
        )

    def unregister(self, key: str) -> None:
        self._items.pop(key, None)
        self._providers.pop(key, None)

    # ==================== 查询 ====================

    def builtin_items(self) -> List[DisplayItem]:
        return sorted(
            [i for i in self._items.values() if not i.prefix],
            key=lambda i: (i.sort_order, i.key),
        )

    def all_items(self) -> List[DisplayItem]:
        return sorted(self._items.values(), key=lambda i: (i.sort_order, i.key))

    def item(self, key: str) -> Optional[DisplayItem]:
        return self._items.get(key)

    def dynamic_prefixes(self) -> List[DisplayItem]:
        return sorted(
            [i for i in self._items.values() if i.prefix], key=lambda i: i.sort_order
        )

    def is_known(self, key: str) -> bool:
        if key in self._items:
            return True
        return self._match_prefix(key) is not None

    def display_name(self, key: str) -> str:
        item = self._items.get(key)
        if item is not None:
            return item.name
        matched = self._match_prefix(key)
        if matched is not None:
            prefix, tail = matched
            base = self._items[prefix].name
            return f"{base}·{tail}"
        return key

    def _match_prefix(self, key: str):
        if ":" not in key:
            return None
        prefix, tail = key.split(":", 1)
        normalized = prefix + ":"
        if normalized in self._prefix_providers:
            return normalized, tail
        return None

    # ==================== 渲染 ====================

    def render(self, key: str, ctx: Any) -> Optional[str]:
        """取某个显示项的当前内容。任何异常都吞掉并返回 None —— 显示崩了不该影响主流程。"""
        provider = self._providers.get(key)
        if provider is not None:
            try:
                return provider(ctx)
            except Exception:
                return None

        matched = self._match_prefix(key)
        if matched is not None:
            prefix, tail = matched
            handler = self._prefix_providers.get(prefix)
            if handler is not None:
                try:
                    return handler(ctx, tail)
                except Exception:
                    return None
        return None

    def render_many(self, keys: List[str], ctx: Any) -> List[tuple]:
        """批量渲染，返回 [(key, 显示名, 内容)]，内容为 None 的会被过滤掉。"""
        out: List[tuple] = []
        for key in keys:
            if not self.is_known(key):
                continue
            text = self.render(key, ctx)
            if text:
                out.append((key, self.display_name(key), text))
        return out
