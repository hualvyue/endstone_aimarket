"""定价模型。

价格全部用整数最小单位 + 基点(bps) 表达：
  * base_buy_minor  —— 基准「玩家买入价」（玩家花这个价从系统商店买）
  * base_sell_minor —— 基准「玩家卖出价」（玩家卖给系统商店拿这个价）
正常情况 base_buy > base_sell，差价就是商店的利润空间（在经济里相当于回收折扣）。

动态调整同样用基点存：
  * demand_*_bps   —— 供需驱动的偏移
  * daily_bps      —— 当日随机波动
  * *_acc_minor    —— 未触发的累计成交额（攒够阈值就跳一档价格，然后清零，避免长期溢出）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PriceState:
    item_key: str
    base_buy_minor: int = 0
    base_sell_minor: int = 0
    demand_buy_bps: int = 0
    demand_sell_bps: int = 0
    daily_bps: int = 0
    buy_acc_minor: int = 0
    sell_acc_minor: int = 0
    version: int = 0
    updated_at_ms: int = 0

    @classmethod
    def from_row(cls, row: dict) -> "PriceState":
        return cls(
            item_key=row["item_key"],
            base_buy_minor=int(row.get("base_buy_minor", 0)),
            base_sell_minor=int(row.get("base_sell_minor", 0)),
            demand_buy_bps=int(row.get("demand_buy_bps", 0)),
            demand_sell_bps=int(row.get("demand_sell_bps", 0)),
            daily_bps=int(row.get("daily_bps", 0)),
            buy_acc_minor=int(row.get("buy_acc_minor", 0)),
            sell_acc_minor=int(row.get("sell_acc_minor", 0)),
            version=int(row.get("version", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
        )

    def base_for(self, direction: str) -> int:
        return self.base_buy_minor if direction == "buy" else self.base_sell_minor


@dataclass
class PriceQuote:
    """一次报价。UI 展示、下单确认、实际结算**必须**用同一个 quote。"""

    item_key: str
    direction: str
    unit_minor: int
    base_minor: int
    demand_bps: int = 0
    daily_bps: int = 0
    discount_bps: int = 0
    price_version: int = 0
    suspended: bool = False
    suspend_reason: str = ""
    breakdown: Dict[str, int] = field(default_factory=dict)

    @property
    def effective_bps(self) -> int:
        return self.demand_bps + self.daily_bps + self.discount_bps

    def describe(self) -> str:
        if self.suspended:
            return f"暂停交易（{self.suspend_reason}）"
        parts = [f"基准 {self.base_minor}"]
        if self.demand_bps:
            parts.append(f"供需 {self.demand_bps / 100:+.2f}%")
        if self.daily_bps:
            parts.append(f"波动 {self.daily_bps / 100:+.2f}%")
        if self.discount_bps:
            parts.append(f"折扣 {self.discount_bps / 100:+.2f}%")
        return "，".join(parts) + f" → {self.unit_minor}"


@dataclass
class PriceRule:
    """某个物品的定价规则（来自价格表配置）。"""

    item_key: str
    display_name: str = ""
    category: str = "其他"
    base_buy_minor: int = 0
    base_sell_minor: int = 0
    enabled: bool = True
