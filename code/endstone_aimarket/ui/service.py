"""自选显示的总装：注册表 + 偏好 + 渲染器。"""
from __future__ import annotations

from typing import Dict, List, Optional

from .prefs import CARRIER_ACTIONBAR, CARRIER_NONE, CARRIER_SIDEBAR, PrefsRepository, UiPrefs
from .providers import register_builtin
from .registry import DisplayItem, DisplayRegistry
from .renderer import DisplayRenderer


class UIService:
    def __init__(self, plugin, db, *, config=None, log=None):
        self.plugin = plugin
        self.config = config
        self.log = log
        self.registry = DisplayRegistry()
        register_builtin(self.registry)
        self.prefs = PrefsRepository(db)
        self.renderer = DisplayRenderer(plugin, self.registry, self.prefs, log)

    # ==================== 默认配置 ====================

    def default_items(self) -> List[str]:
        """没配置过的玩家默认显示什么。"""
        if self.config is not None:
            configured = self.config.get("ui.default_items")
            if isinstance(configured, list) and configured:
                return [str(i) for i in configured]
        return [i.key for i in self.registry.builtin_items() if i.default_on]

    def default_carrier(self) -> str:
        if self.config is not None:
            carrier = str(self.config.get("ui.default_carrier", CARRIER_SIDEBAR))
            if carrier in (CARRIER_SIDEBAR, CARRIER_ACTIONBAR, CARRIER_NONE):
                return carrier
        return CARRIER_SIDEBAR

    # ==================== 生命周期 ====================

    def on_player_join(self, player) -> None:
        """进服立刻渲染一次，不用等下一个刷新周期。"""
        try:
            self.renderer.refresh_player(player)
        except Exception as e:
            if self.log:
                self.log.debug(f"进服渲染失败: {e}")

    def on_player_quit(self, player) -> None:
        self.renderer.clear(player)

    def refresh_all(self) -> int:
        return self.renderer.refresh_all()

    def refresh_player(self, player) -> bool:
        return self.renderer.refresh_player(player)

    # ==================== 偏好读写 ====================

    def get_prefs(self, account_id: str) -> UiPrefs:
        return self.prefs.get_or_default(account_id, self.default_items())

    def items_for_gui(self) -> List[DisplayItem]:
        return self.registry.builtin_items()

    def dynamic_prefixes(self) -> List[DisplayItem]:
        return self.registry.dynamic_prefixes()

    def toggle(self, account_id: str, key: str) -> UiPrefs:
        if not self.registry.is_known(key):
            return self.get_prefs(account_id)
        return self.prefs.toggle_item(account_id, key, default_items=self.default_items())

    def set_carrier(self, account_id: str, carrier: str) -> UiPrefs:
        return self.prefs.set_carrier(account_id, carrier, default_items=self.default_items())

    def set_enabled(self, account_id: str, enabled: bool) -> UiPrefs:
        return self.prefs.set_enabled(account_id, enabled, default_items=self.default_items())

    def clear(self, account_id: str) -> UiPrefs:
        prefs = self.get_prefs(account_id)
        prefs.items = []
        self.prefs.save(prefs)
        return prefs

    def reset_default(self, account_id: str) -> UiPrefs:
        prefs = self.get_prefs(account_id)
        prefs.items = list(self.default_items())
        prefs.carrier = self.default_carrier()
        prefs.enabled = True
        self.prefs.save(prefs)
        return prefs

    def add_dynamic(self, account_id: str, key: str) -> UiPrefs:
        """把 `stock:公司名` 这类动态项加进去。"""
        prefs = self.get_prefs(account_id)
        if key not in prefs.items:
            prefs.items.append(key)
            self.prefs.save(prefs)
        return prefs

    def remove(self, account_id: str, key: str) -> UiPrefs:
        prefs = self.get_prefs(account_id)
        prefs.items = [i for i in prefs.items if i != key]
        self.prefs.save(prefs)
        return prefs

    # ==================== 供 GUI 预览 ====================

    def preview(self, player, account_id: str) -> str:
        """生成一段预览文本，让玩家在面板里直接看到效果。"""
        prefs = self.get_prefs(account_id)
        if not prefs.items:
            return self.plugin.text("ui.prefs.empty")
        from .renderer import DisplayContext

        ctx = DisplayContext(self.plugin, player, account_id)
        rows = self.registry.render_many(prefs.items, ctx)
        if not rows:
            return self.plugin.text("ui.prefs.nothing")
        return "\n".join(f"§7· {text}" for _, _, text in rows)

    def carrier_name(self, carrier: str) -> str:
        return self.plugin.text(f"ui.carrier.{carrier}")
