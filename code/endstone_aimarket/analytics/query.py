"""指标查询 API。

给管理面板、报表用，也是将来 AI 层读数据的入口。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..core.database import Database
from .collector import DAY_MS, GRANULARITY_DAY, GRANULARITY_HOUR, HOUR_MS, MetricsCollector


class MetricsQuery:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 序列 ====================

    def series(
        self,
        metric: str,
        *,
        dimension: str = "global",
        granularity: str = GRANULARITY_HOUR,
        since_ms: Optional[int] = None,
        limit: int = 168,
    ) -> List[Dict]:
        """取某个指标的历史序列（升序）。"""
        if since_ms is None:
            span = DAY_MS if granularity == GRANULARITY_DAY else HOUR_MS
            since_ms = int(time.time() * 1000) - span * limit
        return self.db.query_all(
            """SELECT bucket_ms, value_int, value_real
                 FROM metric_samples
                WHERE metric = ? AND dimension = ? AND granularity = ? AND bucket_ms >= ?
                ORDER BY bucket_ms ASC
                LIMIT ?""",
            (metric, dimension, granularity, int(since_ms), int(limit)),
        )

    def latest(self, metric: str, *, dimension: str = "global", granularity: str = GRANULARITY_HOUR):
        row = self.db.query_one(
            """SELECT value_int, value_real, bucket_ms
                 FROM metric_samples
                WHERE metric = ? AND dimension = ? AND granularity = ?
                ORDER BY bucket_ms DESC LIMIT 1""",
            (metric, dimension, granularity),
        )
        if row is None:
            return None
        return row["value_int"] if row["value_int"] is not None else row["value_real"]

    def latest_int(self, metric: str, *, dimension: str = "global", default: int = 0) -> int:
        value = self.latest(metric, dimension=dimension)
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def latest_float(self, metric: str, *, dimension: str = "global", default: float = 0.0) -> float:
        value = self.latest(metric, dimension=dimension)
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    # ==================== 汇总 ====================

    def overview(self) -> Dict[str, object]:
        """一眼看清当前经济状况。"""
        return {
            "m0_minor": self.latest_int("m0_minor"),
            "minted_minor": self.latest_int("minted_minor"),
            "treasury_minor": self.latest_int("treasury_minor"),
            "escrow_minor": self.latest_int("escrow_minor"),
            "holders": self.latest_int("holders"),
            "accounts": self.latest_int("accounts"),
            "avg_balance": self.latest_int("avg_balance"),
            "median_balance": self.latest_int("median_balance"),
            "gini_balance": self.latest_float("gini_balance"),
            "top10_share_bps": self.latest_int("top10_share_bps"),
            "cpi_bps": self.latest_int("cpi_bps", default=10000),
            "active_shops": self.latest_int("active_shops"),
            "trade_qty": self.latest_int("trade_qty"),
            "trade_gross": self.latest_int("trade_gross"),
        }

    def inflation_bps(self, *, lookback: int = 24) -> int:
        """相对 lookback 个周期前的 CPI 变化（基点）。正数=通胀。"""
        rows = self.series("cpi_bps", limit=lookback + 1)
        if len(rows) < 2:
            return 0
        first = int(rows[0]["value_int"] or 10000)
        last = int(rows[-1]["value_int"] or 10000)
        if first <= 0:
            return 0
        return (last - first) * 10000 // first

    # ==================== 维度 ====================

    def top_items(self, *, metric: str = "trade_gross", limit: int = 10,
                  granularity: str = GRANULARITY_HOUR) -> List[Dict]:
        """按指标排出最活跃的物品。"""
        return self.db.query_all(
            """SELECT dimension, value_int, bucket_ms
                 FROM metric_samples
                WHERE metric = ? AND dimension != 'global' AND granularity = ?
                ORDER BY bucket_ms DESC, value_int DESC
                LIMIT ?""",
            (metric, granularity, int(limit)),
        )

    def item_price_series(self, item_key: str, *, direction: str = "buy", limit: int = 168) -> List[Dict]:
        metric = "price_buy" if direction == "buy" else "price_sell"
        return self.series(metric, dimension=item_key, limit=limit)

    # ==================== 原始数据 ====================

    def recent_trades(self, *, limit: int = 50) -> List[Dict]:
        return self.db.query_all(
            """SELECT * FROM shop_trades
                WHERE status = 'filled'
                ORDER BY created_at_ms DESC LIMIT ?""",
            (int(limit),),
        )

    def recent_risk_flags(self, *, limit: int = 50, days: int = 7) -> List[Dict]:
        since = int(time.time() * 1000) - days * DAY_MS
        return self.db.query_all(
            "SELECT * FROM risk_flags WHERE created_at_ms >= ? ORDER BY created_at_ms DESC LIMIT ?",
            (since, int(limit)),
        )

    def account_ranking(self, *, limit: int = 20) -> List[Dict]:
        return self.db.query_all(
            """SELECT a.account_id, a.current_name, a.balance_minor, a.held_minor
                 FROM accounts a
                WHERE a.kind = 'player'
                ORDER BY a.balance_minor DESC LIMIT ?""",
            (int(limit),),
        )

    def format_report(self, *, money_formatter=None) -> str:
        """给管理面板用的文本报表。"""
        fmt = money_formatter or (lambda v: str(v))
        data = self.overview()
        lines = [
            "===== aimarket 经济总览 =====",
            f"货币供应量 M0 : {fmt(data['m0_minor'])}",
            f"累计发行      : {fmt(data['minted_minor'])}",
            f"国库结余      : {fmt(data['treasury_minor'])}",
            f"商店托管沉淀  : {fmt(data['escrow_minor'])}",
            f"账户 / 持仓户 : {data['accounts']} / {data['holders']}",
            f"人均余额      : {fmt(data['avg_balance'])}",
            f"余额中位数    : {fmt(data['median_balance'])}",
            f"基尼系数      : {data['gini_balance']:.4f}",
            f"前 10% 持有   : {data['top10_share_bps'] / 100:.2f}%",
            f"物价指数 CPI  : {data['cpi_bps'] / 100:.2f}（基准 100.00）",
            f"近期通胀      : {self.inflation_bps() / 100:+.2f}%",
            f"在营商店      : {data['active_shops']}",
            f"近期成交      : {data['trade_qty']} 件 / {fmt(data['trade_gross'])}",
        ]
        return "\n".join(lines)
