"""经济指标采集器。

这一层是「先把工具开发起来」的落点：AI 部分这轮不做，但 AI 将来要吃的数据
必须从第一天就开始攒 —— 等想做分析时再补历史数据是补不出来的。

采集的指标
    m0_minor          货币供应量（玩家手上持有的货币总量）
    minted_minor      累计净发行量
    treasury_minor    国库余额
    escrow_minor      各商店托管沉淀的资金
    holders           持有货币的玩家数
    avg_balance       人均余额
    median_balance    余额中位数
    gini_balance      余额基尼系数（0 完全平均，1 极端集中）
    cpi_bps           固定篮子物价指数（基准期 = 10000）
    active_shops      在营商店数
    trade_qty         成交件数
    trade_gross       成交金额
    每物品的 trade_qty / trade_gross 与 price_buy / price_sell

全部写进 metric_samples，主键是 (时间桶, 粒度, 指标名, 维度)，
将来加新指标不需要改表结构。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.database import Database
from ..core.money import apply_bps

GRANULARITY_HOUR = "hour"
GRANULARITY_DAY = "day"

SECOND_MS = 1000
HOUR_MS = 3600 * SECOND_MS
DAY_MS = 24 * HOUR_MS


class MetricsCollector:
    def __init__(self, db: Database, economy, pricing, shop_repo=None, log=None):
        self.db = db
        self.economy = economy
        self.pricing = pricing
        self.shop_repo = shop_repo
        self.log = log

    # ==================== 时间桶 ====================

    @staticmethod
    def bucket_ms(when_ms: Optional[int] = None, granularity: str = GRANULARITY_HOUR) -> int:
        now = int(when_ms if when_ms is not None else time.time() * 1000)
        span = DAY_MS if granularity == GRANULARITY_DAY else HOUR_MS
        return (now // span) * span

    # ==================== 采集 ====================

    def collect(self, *, granularity: str = GRANULARITY_HOUR, when_ms: Optional[int] = None) -> int:
        """采一轮指标。返回写入的样本条数。"""
        bucket = self.bucket_ms(when_ms, granularity)
        written = 0
        try:
            written += self._collect_money(bucket, granularity)
            written += self._collect_distribution(bucket, granularity)
            written += self._collect_shops(bucket, granularity)
            written += self._collect_market(bucket, granularity, when_ms)
            written += self._collect_cpi(bucket, granularity)
        except Exception as e:
            if self.log:
                self.log.error(f"指标采集失败: {e}")
        return written

    def _put(
        self,
        bucket: int,
        granularity: str,
        metric: str,
        dimension: str,
        *,
        value_int: Optional[int] = None,
        value_real: Optional[float] = None,
        details: Optional[dict] = None,
    ) -> int:
        payload = {
            "bucket_ms": int(bucket),
            "granularity": granularity,
            "metric": metric,
            "dimension": dimension,
            "value_int": int(value_int) if value_int is not None else None,
            "value_real": float(value_real) if value_real is not None else None,
            "details_json": json.dumps(details, ensure_ascii=False) if details else None,
        }
        try:
            self.db.execute(
                """INSERT INTO metric_samples
                       (bucket_ms, granularity, metric, dimension, value_int, value_real, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bucket_ms, granularity, metric, dimension) DO UPDATE SET
                       value_int = excluded.value_int,
                       value_real = excluded.value_real,
                       details_json = excluded.details_json""",
                (
                    payload["bucket_ms"], payload["granularity"], payload["metric"],
                    payload["dimension"], payload["value_int"], payload["value_real"],
                    payload["details_json"],
                ),
            )
            return 1
        except Exception as e:
            if self.log:
                self.log.debug(f"指标 {metric}/{dimension} 写入失败: {e}")
            return 0

    # ---------- 货币 ----------

    def _collect_money(self, bucket: int, granularity: str) -> int:
        count = 0
        count += self._put(bucket, granularity, "m0_minor", "global",
                           value_int=self.economy.total_supply())
        count += self._put(bucket, granularity, "minted_minor", "global",
                           value_int=self.economy.minted_total())
        count += self._put(bucket, granularity, "treasury_minor", "global",
                           value_int=self.economy.treasury_balance())
        escrow = int(
            self.db.scalar(
                "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts WHERE kind = 'escrow'",
                default=0,
            ) or 0
        )
        count += self._put(bucket, granularity, "escrow_minor", "global", value_int=escrow)
        return count

    # ---------- 分布 ----------

    def _collect_distribution(self, bucket: int, granularity: str) -> int:
        rows = self.db.query_all(
            "SELECT balance_minor FROM accounts WHERE kind = 'player' ORDER BY balance_minor"
        )
        balances = [int(r["balance_minor"]) for r in rows]
        holders = [b for b in balances if b > 0]
        total = sum(balances)
        n = len(balances)
        count = 0

        count += self._put(bucket, granularity, "holders", "global", value_int=len(holders))
        count += self._put(bucket, granularity, "accounts", "global", value_int=n)
        if n == 0:
            return count

        count += self._put(bucket, granularity, "avg_balance", "global",
                           value_int=(total // n) if n else 0)
        count += self._put(bucket, granularity, "median_balance", "global",
                           value_int=self._median(balances))
        count += self._put(bucket, granularity, "top10_share_bps", "global",
                           value_int=self._top_share_bps(balances, 0.1))
        count += self._put(bucket, granularity, "gini_balance", "global",
                           value_real=self._gini(balances))
        return count

    @staticmethod
    def _median(sorted_values: List[int]) -> int:
        if not sorted_values:
            return 0
        n = len(sorted_values)
        mid = n // 2
        if n % 2 == 1:
            return int(sorted_values[mid])
        return int((sorted_values[mid - 1] + sorted_values[mid]) // 2)

    @staticmethod
    def _top_share_bps(sorted_values: List[int], ratio: float) -> int:
        """头部玩家持有的货币占比（基点）。集中度过高的预警指标。"""
        total = sum(sorted_values)
        if total <= 0:
            return 0
        k = max(1, int(len(sorted_values) * ratio))
        top = sum(sorted_values[-k:])
        return int(top * 10000 // total)

    @staticmethod
    def _gini(sorted_values: List[int]) -> float:
        """基尼系数。输入需已升序。"""
        n = len(sorted_values)
        if n <= 1:
            return 0.0
        total = sum(sorted_values)
        if total <= 0:
            return 0.0
        cumulative = 0
        weighted = 0
        for index, value in enumerate(sorted_values, start=1):
            cumulative += value
            weighted += index * value
        gini = (2 * weighted) / (n * total) - (n + 1) / n
        return round(max(0.0, min(1.0, gini)), 6)

    # ---------- 商店 ----------

    def _collect_shops(self, bucket: int, granularity: str) -> int:
        if self.shop_repo is None:
            return 0
        stats = self.shop_repo.stats()
        count = self._put(bucket, granularity, "active_shops", "global",
                          value_int=stats["active"])
        count += self._put(bucket, granularity, "infinite_shops", "global",
                           value_int=stats["infinite"])
        for shop_type, n in self.shop_repo.types_breakdown().items():
            count += self._put(bucket, granularity, "shops_by_type", shop_type, value_int=int(n))
        return count

    # ---------- 市场 ----------

    def _collect_market(self, bucket: int, granularity: str, when_ms: Optional[int]) -> int:
        span = DAY_MS if granularity == GRANULARITY_DAY else HOUR_MS
        since = int((when_ms if when_ms is not None else time.time() * 1000)) - span
        rows = self.db.query_all(
            """SELECT item_key, side,
                      SUM(quantity) AS qty,
                      SUM(gross_minor) AS gross
                 FROM market_history
                WHERE event_type = 'trade' AND occurred_at_ms >= ?
                GROUP BY item_key, side""",
            (since,),
        )
        count = 0
        total_qty = 0
        total_gross = 0
        for row in rows:
            item_key = row["item_key"]
            qty = int(row["qty"] or 0)
            gross = int(row["gross"] or 0)
            total_qty += qty
            total_gross += gross
            count += self._put(bucket, granularity, "trade_qty", item_key, value_int=qty)
            count += self._put(bucket, granularity, "trade_gross", item_key, value_int=gross)
        count += self._put(bucket, granularity, "trade_qty", "global", value_int=total_qty)
        count += self._put(bucket, granularity, "trade_gross", "global", value_int=total_gross)

        # 当前报价快照，供后续做价格走势
        for item_key in self.pricing.rules():
            for direction, metric in (("buy", "price_buy"), ("sell", "price_sell")):
                value = self.pricing.quote_price(item_key, direction)
                if value is not None:
                    count += self._put(bucket, granularity, metric, item_key, value_int=value)
        return count

    # ---------- 物价指数 ----------

    def _collect_cpi(self, bucket: int, granularity: str) -> int:
        """固定篮子物价指数。

        篮子里的每个物品以它自己的基准买入价为权重，比较当前买入价与基准价：
            CPI = Σ( 基准价_i × 当前价_i/基准价_i ) / Σ(基准价_i) × 10000
        基准期 = 10000。>10000 表示整体变贵（通胀），<10000 表示变便宜。
        全程整数运算，避免浮点误差在长期累积中把指数带偏。
        """
        rules = self.pricing.rules()
        if not rules:
            return 0

        weight_total = 0
        weighted_sum = 0
        used = 0
        for item_key, rule in rules.items():
            if not rule.enabled or rule.base_buy_minor <= 0:
                continue
            current = self.pricing.quote_price(item_key, "buy")
            if not current:
                continue
            base = int(rule.base_buy_minor)
            weight_total += base
            weighted_sum += base * (int(current) * 10000 // base)
            used += 1

        if used == 0 or weight_total <= 0:
            return 0
        index = weighted_sum // weight_total
        return self._put(
            bucket, granularity, "cpi_bps", "global",
            value_int=index,
            details={"items": used, "weight_total": weight_total},
        )

    # ==================== 清理 ====================

    def cleanup(self, *, keep_days: int = 90) -> int:
        cutoff = int(time.time() * 1000) - keep_days * DAY_MS
        removed = self.db.delete("metric_samples", "bucket_ms < ?", (cutoff,))
        removed += self.db.delete("risk_flags", "created_at_ms < ?", (cutoff,))
        return removed

    def cleanup_market_history(self, *, keep_days: int = 30) -> int:
        cutoff = int(time.time() * 1000) - keep_days * DAY_MS
        return self.db.delete("market_history", "occurred_at_ms < ?", (cutoff,))
