"""定价引擎：官方指导价 + 供需驱动调价 + 每日波动 + 向基准回归。

两条不变量
------------------------------------------------------------------
1. `record_trade` 只能在**成交的同一数据库事务内**调用：物价变动必须与
   实际扣款/发货原子绑定，任何一笔失败交易都不得污染价格。
2. **唯一取价入口 get_quote()**：UI 展示、下单确认、实际结算全部走它，
   不允许任何地方自己再算一遍，保证玩家看到的价格与实扣一致。
"""
from __future__ import annotations

import random
import time
from typing import Dict, Optional

from ..core.constants import TradeDirection
from ..core.errors import ErrorCode
from ..core.money import minor_per_unit
from ..core.result import Result
from .models import PriceQuote, PriceRule
from .repository import MarketRepository, now_ms

# 价格表中的基准价一律用「显示单位」为单位书写，载入时换算成最小单位
# （换算比随货币配置运行时注入，见 core.money.minor_per_unit）

DEFAULT_DYNAMIC = {
    "enabled": True,
    "step_amount_minor": 1_000_000,   # 每累计 1 万金币的成交额，跳一档
    "step_bps": 100,                   # 一档 = 1%
    "max_buy_increase_bps": 5000,      # 买入价最多上浮 50%
    "max_sell_decrease_bps": 3000,     # 卖出价最多下浮 30%
    "sell_buy_link_ratio_bps": 5000,   # 买入价上涨的 50% 联动到卖出价
    "recovery_bps_per_hour": 2,        # 每小时回归 0.02%
}

DEFAULT_DAILY = {
    "enabled": True,
    "item_count": 3,
    "min_bps": -1500,
    "max_bps": 1500,
    "reset_hour": 0,
}


class PricingEngine:
    def __init__(self, db, config=None, log=None):
        self.db = db
        self.log = log
        self.repo = MarketRepository(db)
        self.config = config
        self._rules: Dict[str, PriceRule] = {}
        self._categories: list = []
        self._last_daily_date = None
        self.price_table_error: str = ""

    # ==================== 价格表 ====================

    def load_price_table(self, table: Optional[dict]) -> None:
        """载入官方价格表。table 结构见 resources/prices.json。"""
        self._rules = {}
        self._categories = []
        self.price_table_error = ""
        if not table:
            self.price_table_error = "价格表为空"
            if self.log:
                self.log.warning("官方价格表为空，官方定价商店将无法使用")
            return

        for entry in table.get("categories", []) or []:
            if isinstance(entry, dict) and entry.get("key"):
                self._categories.append(
                    {"key": str(entry["key"]), "name": str(entry.get("name", entry["key"]))}
                )

        items = table.get("items") or {}
        if not isinstance(items, dict):
            self.price_table_error = "价格表 items 字段格式错误"
            return

        for item_key, raw in items.items():
            if not isinstance(raw, dict):
                continue
            try:
                buy = raw.get("buy")
                sell = raw.get("sell")
                buy_minor = _to_minor(buy) if buy is not None else 0
                sell_minor = _to_minor(sell) if sell is not None else 0
                if buy_minor <= 0 and sell_minor <= 0:
                    continue
                # 允许只配一边：另一边自动按 50% 推导，保证回收价永远低于买入价
                if buy_minor <= 0:
                    buy_minor = sell_minor * 2
                if sell_minor <= 0:
                    sell_minor = buy_minor // 2
                if sell_minor >= buy_minor:
                    sell_minor = max(1, buy_minor // 2)
                self._rules[str(item_key)] = PriceRule(
                    item_key=str(item_key),
                    display_name=str(raw.get("name") or _fallback_name(item_key)),
                    category=str(raw.get("category") or "其他"),
                    base_buy_minor=int(buy_minor),
                    base_sell_minor=int(sell_minor),
                    enabled=bool(raw.get("enabled", True)),
                )
            except Exception as e:
                if self.log:
                    self.log.warning(f"价格表条目 {item_key} 解析失败，已跳过: {e}")

        if self.log:
            self.log.info(f"官方价格表载入完成：{len(self._rules)} 个物品，{len(self._categories)} 个分类")
        if not self._rules:
            self.price_table_error = "没有解析出任何有效物品"

    def reload_table(self, table: Optional[dict]) -> None:
        self.load_price_table(table)

    def has_rule(self, item_key: str) -> bool:
        rule = self._rules.get(item_key)
        return bool(rule and rule.enabled)

    def rules(self) -> Dict[str, PriceRule]:
        return dict(self._rules)

    def categories(self) -> list:
        return list(self._categories)

    def display_name(self, item_key: str) -> str:
        rule = self._rules.get(item_key)
        if rule and rule.display_name:
            return rule.display_name
        return _fallback_name(item_key)

    def category_of(self, item_key: str) -> str:
        rule = self._rules.get(item_key)
        return rule.category if rule else "其他"

    def items_in_category(self, category: str) -> Dict[str, PriceRule]:
        return {
            key: rule
            for key, rule in self._rules.items()
            if rule.category == category and rule.enabled
        }

    def ensure_states(self) -> None:
        """把价格表同步进价格状态表。启动时调用一次即可。"""
        with self.db.transaction():
            for key, rule in self._rules.items():
                self.repo.ensure_state(key, rule.base_buy_minor, rule.base_sell_minor)

    # ==================== 报价（唯一价格来源）====================

    def get_quote(
        self,
        item_key: str,
        direction: str,
        *,
        discount_bps: int = 0,
        state=None,
    ) -> Result[PriceQuote]:
        """给出报价。direction 用 'buy'（玩家买入）/ 'sell'（玩家卖出）。"""
        rule = self._rules.get(item_key)
        if rule is None or not rule.enabled:
            return Result.failure(ErrorCode.PRICE_NOT_SET, f"{item_key} 没有官方定价")

        if state is None:
            state = self.repo.get_state(item_key)
            if state is None:
                state = self.repo.ensure_state(item_key, rule.base_buy_minor, rule.base_sell_minor)

        quote = self._compute(rule, state, direction, discount_bps)

        # 防御：回收价不得高于或等于买入价，否则玩家可以无限套利
        if direction == TradeDirection.SELL.value and not quote.suspended:
            buy_quote = self._compute(rule, state, TradeDirection.BUY.value, discount_bps)
            if quote.unit_minor >= buy_quote.unit_minor:
                quote.suspended = True
                quote.suspend_reason = "回收价已达到或超过出售价"
        return Result.success(quote)

    def _compute(self, rule: PriceRule, state, direction: str, discount_bps: int) -> PriceQuote:
        is_buy = direction == TradeDirection.BUY.value
        base = state.base_buy_minor if is_buy else state.base_sell_minor
        demand = state.demand_buy_bps if is_buy else state.demand_sell_bps
        total_bps = int(demand) + int(state.daily_bps) + int(discount_bps)
        unit = (int(base) * (10000 + total_bps)) // 10000
        unit = max(1, int(unit))
        return PriceQuote(
            item_key=rule.item_key,
            direction=direction,
            unit_minor=unit,
            base_minor=int(base),
            demand_bps=int(demand),
            daily_bps=int(state.daily_bps),
            discount_bps=int(discount_bps),
            price_version=int(state.version),
            breakdown={
                "base": int(base),
                "demand_bps": int(demand),
                "daily_bps": int(state.daily_bps),
                "discount_bps": int(discount_bps),
            },
        )

    def quote_price(self, item_key: str, direction: str, *, discount_bps: int = 0) -> Optional[int]:
        """只要价格数字，失败返回 None。"""
        result = self.get_quote(item_key, direction, discount_bps=discount_bps)
        if not result.ok or result.value.suspended:
            return None
        return result.value.unit_minor

    # ==================== 成交记录（只能在 Confirm 事务内调用）====================

    def record_trade(
        self,
        item_key: str,
        direction: str,
        *,
        quantity: int,
        gross_minor: int,
        unit_minor: Optional[int] = None,
        trade_id: Optional[str] = None,
    ) -> None:
        """记录一次**已成功**的成交，并推进供需价格。

        调用约束：必须处于成交确认的同一个数据库事务中。
        这样交易回滚时价格调整也一并回滚，失败交易不会污染物价。
        """
        self.repo.record_event(
            event_type="trade",
            item_key=item_key,
            side=direction,
            quantity=quantity,
            gross_minor=gross_minor,
            unit_minor=unit_minor,
            source_trade_id=trade_id,
        )

        dynamic = self._dynamic()
        if not dynamic["enabled"]:
            return

        state = self.repo.get_state(item_key)
        if state is None:
            rule = self._rules.get(item_key)
            if rule is None:
                return
            state = self.repo.ensure_state(item_key, rule.base_buy_minor, rule.base_sell_minor)

        step_amount = max(1, int(dynamic["step_amount_minor"]))
        step_bps = max(1, int(dynamic["step_bps"]))

        if direction == TradeDirection.BUY.value:
            # 玩家买入 = 需求增加 → 买入价上涨
            state.buy_acc_minor += int(gross_minor)
            steps = state.buy_acc_minor // step_amount
            if steps > 0:
                state.buy_acc_minor -= steps * step_amount
                before = state.demand_buy_bps
                state.demand_buy_bps = min(
                    int(dynamic["max_buy_increase_bps"]),
                    state.demand_buy_bps + int(steps) * step_bps,
                )
                gained = state.demand_buy_bps - before
                # 买入价上涨时，回收价联动上调，鼓励玩家把货卖回来
                if gained > 0:
                    link = (gained * int(dynamic["sell_buy_link_ratio_bps"])) // 10000
                    if link:
                        state.demand_sell_bps = min(
                            int(dynamic["max_buy_increase_bps"]),
                            state.demand_sell_bps + link,
                        )
        else:
            # 玩家卖出 = 供给增加 → 卖出（回收）价下跌
            state.sell_acc_minor += int(gross_minor)
            steps = state.sell_acc_minor // step_amount
            if steps > 0:
                state.sell_acc_minor -= steps * step_amount
                state.demand_sell_bps = max(
                    -int(dynamic["max_sell_decrease_bps"]),
                    state.demand_sell_bps - int(steps) * step_bps,
                )

        self.repo.upsert_state(state)

    # ==================== 每日波动 ====================

    def maybe_apply_daily(self, *, now: Optional[time.struct_time] = None) -> int:
        """按配置在每天固定时刻抖动一批物品的价格。返回被抖动的物品数。"""
        daily = self._daily()
        if not daily["enabled"]:
            return 0

        current = time.localtime(now) if now else time.localtime()
        today = (current.tm_year, current.tm_mon, current.tm_mday)
        if self._last_daily_date == today:
            return 0
        if current.tm_hour < int(daily["reset_hour"]):
            return 0

        keys = [k for k, r in self._rules.items() if r.enabled]
        if not keys:
            return 0

        chosen = random.sample(keys, min(int(daily["item_count"]), len(keys)))
        with self.db.transaction():
            # 先把所有物品的日波动清零，再给被选中的物品重新赋值
            for key in keys:
                state = self.repo.get_state(key)
                if state is None:
                    continue
                if state.daily_bps != 0:
                    self.repo.set_daily(key, 0, state)

            for key in chosen:
                rule = self._rules[key]
                state = self.repo.get_state(key)
                if state is None:
                    state = self.repo.ensure_state(key, rule.base_buy_minor, rule.base_sell_minor)
                bps = random.randint(int(daily["min_bps"]), int(daily["max_bps"]))
                self.repo.set_daily(key, bps, state)
                self.repo.record_event(
                    event_type="daily", item_key=key, side=None, quantity=0,
                    gross_minor=0, unit_minor=None,
                )

        self._last_daily_date = today
        if self.log:
            self.log.info(f"已对 {len(chosen)} 个物品应用每日波动")
        return len(chosen)

    # ==================== 价格回归 ====================

    def apply_recovery(self) -> int:
        """每小时调用：需求偏移向 0 回归，避免价格长期卡在极值。"""
        dynamic = self._dynamic()
        if not dynamic["enabled"]:
            return 0
        rate = int(dynamic["recovery_bps_per_hour"])
        if rate <= 0:
            return 0

        changed = 0
        with self.db.transaction():
            for state in self.repo.all_states():
                touched = False
                if state.demand_buy_bps > 0:
                    state.demand_buy_bps = max(0, state.demand_buy_bps - rate)
                    touched = True
                elif state.demand_buy_bps < 0:
                    state.demand_buy_bps = min(0, state.demand_buy_bps + rate)
                    touched = True

                if state.demand_sell_bps > 0:
                    state.demand_sell_bps = max(0, state.demand_sell_bps - rate)
                    touched = True
                elif state.demand_sell_bps < 0:
                    state.demand_sell_bps = min(0, state.demand_sell_bps + rate)
                    touched = True

                if touched:
                    self.repo.upsert_state(state)
                    changed += 1
        return changed

    def reset_adjustments(self) -> int:
        return self.repo.reset_all_adjustments()

    # ==================== 报告 ====================

    def daily_summary(self) -> str:
        lines = []
        for state in self.repo.all_states():
            if state.daily_bps:
                name = self.display_name(state.item_key)
                lines.append(f"{name}: {state.daily_bps / 100:+.1f}%")
        return "今日暂无波动" if not lines else "\n".join(lines)

    def price_report(self, item_key: str, *, discount_bps: int = 0) -> list:
        rows = []
        for direction, label in ((TradeDirection.BUY.value, "玩家买入"), (TradeDirection.SELL.value, "玩家卖出")):
            result = self.get_quote(item_key, direction, discount_bps=discount_bps)
            if not result.ok:
                rows.append(f"{label}: 未定价")
                continue
            rows.append(f"{label}: {result.value.describe()}")
        return rows

    # ==================== 配置读取 ====================

    def _dynamic(self) -> dict:
        merged = dict(DEFAULT_DYNAMIC)
        if self.config is not None:
            section = self.config.section("market.dynamic")
            for key in merged:
                if key in section:
                    merged[key] = section[key]
        return merged

    def _daily(self) -> dict:
        merged = dict(DEFAULT_DAILY)
        if self.config is not None:
            section = self.config.section("market.daily")
            for key in merged:
                if key in section:
                    merged[key] = section[key]
        return merged


def _to_minor(value) -> int:
    """把价格表里的金币数换算成最小单位。支持 100 / 100.5 / '100'。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * minor_per_unit()
    try:
        from decimal import Decimal, ROUND_HALF_UP

        dec = Decimal(str(value))
        return int((dec * minor_per_unit()).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except Exception:
        return 0


def _fallback_name(item_key: str) -> str:
    short = item_key.split(":")[-1] if ":" in item_key else item_key
    return short.replace("_", " ").title()
