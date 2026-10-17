"""把玩家勾选的显示项渲染到侧边栏 / 行动栏。

刷新策略：默认 40 tick（2 秒）一次，**只刷在线、且确实配置了显示内容的玩家**。
绝不每 tick 全量刷 —— 那是这类插件最经典的性能事故来源。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.money import format_amount
from .prefs import CARRIER_ACTIONBAR, CARRIER_NONE, CARRIER_SIDEBAR, UiPrefs

OBJECTIVE_NAME = "__AIMARKET_HUD__"
MAX_LINES = 14

try:
    from endstone.scoreboard import Criteria, DisplaySlot
except ImportError:  # pragma: no cover
    try:
        from endstone import Criteria, DisplaySlot
    except ImportError:
        Criteria = None
        DisplaySlot = None


class DisplayContext:
    """交给 provider 的上下文。provider 只依赖它，不直接碰插件内部。"""

    def __init__(self, plugin, player, account_id: str):
        self.plugin = plugin
        self.player = player
        self.account_id = account_id

    # 常用快捷方式，让 provider 写起来短一些
    @property
    def economy(self):
        return self.plugin.economy_service

    @property
    def companies(self):
        return self.plugin.company_service

    @property
    def stock(self):
        return self.plugin.stock_service

    @property
    def loans(self):
        return self.plugin.loan_service

    @property
    def shop(self):
        return self.plugin.shop_service

    @property
    def bank(self):
        return getattr(self.plugin, "bank_service", None)

    @property
    def bank_deposit(self):
        return getattr(self.plugin, "bank_deposit", None)

    @property
    def bank_lending(self):
        return getattr(self.plugin, "bank_lending", None)

    def balance_minor(self) -> int:
        return self.economy.get_balance(self.account_id).balance_minor


class DisplayRenderer:
    def __init__(self, plugin, registry, prefs, log=None):
        self.plugin = plugin
        self.registry = registry
        self.prefs = prefs
        self.log = log
        self._last_objective: Dict[str, Any] = {}

    # ==================== 定时刷新 ====================

    def refresh_all(self) -> int:
        """由定时任务调用。返回刷新过的玩家数。"""
        refreshed = 0
        for player in self._online_players():
            try:
                if self.refresh_player(player):
                    refreshed += 1
            except Exception as e:
                if self.log:
                    self.log.debug(f"刷新显示失败 {getattr(player, 'name', '?')}: {e}")
        return refreshed

    def refresh_player(self, player) -> bool:
        account = self.plugin.economy_api.api_get_account(
            xuid=_attr(player, "xuid"), name=_attr(player, "name")
        )
        if account is None:
            return False

        prefs = self.prefs.get(account.account_id)
        if prefs is None or not prefs.is_visible:
            # 没配置或关掉了 → 顺手清理掉残留的侧边栏
            self.clear(player)
            return False

        ctx = DisplayContext(self.plugin, player, account.account_id)
        rows = self.registry.render_many(prefs.items, ctx)
        if not rows:
            self.clear(player)
            return False
        return self._render(player, prefs, rows)

    # ==================== 实际渲染 ====================

    def _render(self, player, prefs: UiPrefs, rows: List[tuple]) -> bool:
        lines = [t for _, _, t in rows][:MAX_LINES]
        title = self.plugin.text("ui.hud.title")

        if prefs.carrier == CARRIER_ACTIONBAR:
            self._clear_sidebar(player)
            try:
                player.send_tip("\n".join(lines))
                return True
            except Exception:
                return False

        if prefs.carrier == CARRIER_NONE:
            self._clear_sidebar(player)
            return False

        return self._render_sidebar(player, title, lines)

    def _render_sidebar(self, player, title: str, lines: List[str]) -> bool:
        if Criteria is None or DisplaySlot is None:
            return False
        try:
            scoreboard = player.scoreboard
            # player.scoreboard 永不为 None（未设置时返回服务器全局板），
            # 必须用 is 判断，否则所有玩家会共用一块板、互相覆盖
            if scoreboard is self.plugin.server.scoreboard:
                scoreboard = self.plugin.server.create_scoreboard()
                player.scoreboard = scoreboard

            objective = scoreboard.get_objective(OBJECTIVE_NAME)
            if objective is not None:
                objective.unregister()
            objective = scoreboard.add_objective(OBJECTIVE_NAME, Criteria.DUMMY, title)

            seen = set()
            for index, line in enumerate(lines):
                text = str(line)
                # 计分板同一行内容不能重复，否则会互相覆盖
                if text in seen:
                    text = text + " " * (index % 3 + 1)
                seen.add(text)
                objective.get_score(text).value = len(lines) - index
            objective.set_display(DisplaySlot.SIDE_BAR)
            self._last_objective[getattr(player, "name", "?")] = objective
            return True
        except Exception as e:
            if self.log:
                self.log.debug(f"渲染侧边栏失败: {e}")
            return False

    def _clear_sidebar(self, player) -> None:
        try:
            objective = player.scoreboard.get_objective(OBJECTIVE_NAME)
            if objective is not None:
                objective.unregister()
        except Exception:
            pass

    def clear(self, player) -> bool:
        self._clear_sidebar(player)
        self._last_objective.pop(getattr(player, "name", "?"), None)
        return True

    def clear_all(self) -> int:
        count = 0
        for player in self._online_players():
            if self.clear(player):
                count += 1
        return count

    # ==================== 工具 ====================

    def _online_players(self):
        try:
            return list(self.plugin.server.online_players)
        except Exception:
            return []


def _attr(obj, name: str) -> Optional[str]:
    try:
        value = getattr(obj, name, None)
        return str(value) if value is not None else None
    except Exception:
        return None
