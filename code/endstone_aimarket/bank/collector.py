"""央行指标采集。

与 `analytics` 的采集器**共用 `metric_samples` 表**，但指标是货币口径的。
拆成独立类是为了让"货币供应与金融风险"这一组指标能独立演进，互不干扰。

其中 `base_money_minor` 是**挤兑监测的关键**：`oversupply_bps` 要靠它的历史值
来算基础货币增速。它不依赖滞后的 CPI —— 挤兑刚发生时物价还没动，但基础货币已经跳了。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.database import Database
from .repository import DAY_MS, HOUR_MS, now_ms
from .service import CentralBankService


class BankMetricsCollector:
    def __init__(
        self,
        db: Database,
        bank: CentralBankService,
        *,
        policy_engine=None,
        metrics_query=None,
        log=None,
    ):
        self.db = db
        self.bank = bank
        self.policy = policy_engine
        self.metrics_query = metrics_query
        self.log = log

    # ==================== 基础写入 ====================

    def _bucket(self, when_ms: int, granularity: str) -> int:
        span = DAY_MS if granularity == "day" else HOUR_MS
        return (int(when_ms) // span) * span

    def _put(
        self,
        bucket: int,
        granularity: str,
        metric: str,
        *,
        value_int: int = 0,
        dimension: str = "global",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        import json

        self.db.execute(
            """INSERT INTO metric_samples
                   (bucket_ms, granularity, metric, dimension, value_int, value_real, details_json)
               VALUES (?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(bucket_ms, granularity, metric, dimension)
               DO UPDATE SET value_int = excluded.value_int,
                             details_json = excluded.details_json""",
            (
                int(bucket),
                granularity,
                metric,
                dimension,
                int(value_int),
                json.dumps(details, ensure_ascii=False) if details else None,
            ),
        )
        return 1

    # ==================== 采集 ====================

    def collect(self, *, granularity: str = "hour", when_ms: Optional[int] = None) -> int:
        current = int(when_ms if when_ms is not None else now_ms())
        bucket = self._bucket(current, granularity)
        sheet = self.bank.balance_sheet()
        policy = self.bank.policy()
        count = 0

        def put(metric: str, value: int, **kwargs) -> None:
            nonlocal count
            count += self._put(bucket, granularity, metric, value_int=value, **kwargs)

        # —— 货币口径 ——
        put("base_money_minor", sheet.base_money_minor)
        put("m1_minor", sheet.m1_minor)
        put("m2_minor", sheet.m2_minor)
        put("multiplier_bps", sheet.multiplier_bps)

        # —— 存款结构 ——
        put("deposits_minor", sheet.deposit_total_minor)
        put("deposit_demand_minor", sheet.demand_deposits_minor)
        put("deposit_time_minor", sheet.time_deposits_minor)
        put("deposit_wealth_minor", sheet.wealth_deposits_minor)
        put("deposit_accounts", sheet.deposit_count)

        # —— 资产与资本 ——
        put("bank_reserves_minor", sheet.reserves_minor)
        put("bank_credit_minor", sheet.credit_total_minor)
        put("bank_capital_minor", sheet.capital_minor)
        put("car_bps", sheet.car_bps)

        # —— 政策参数 ——
        put("policy_rate_bps", policy.base_rate_bps_per_day)
        put("rrr_bps", policy.rrr_bps)
        put("inflation_target_bps", policy.target_inflation_bps)

        # —— 风险 ——
        put("run_index_bps", self.bank.run_index_bps(now=current))
        put("oversupply_bps", self.bank.oversupply_bps(now=current))
        put("writeoff_minor", self.bank.repo.writeoff_total())

        # —— 现金流（供挤兑指数与流动性分析）——
        since = current - HOUR_MS
        inflow = int(self.db.scalar(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM bank_events "
            "WHERE kind = 'deposit' AND created_at_ms >= ?", (since,), default=0) or 0)
        outflow = int(self.db.scalar(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM bank_events "
            "WHERE kind = 'withdraw' AND created_at_ms >= ?", (since,), default=0) or 0)
        put("deposit_inflow_minor", inflow)
        put("deposit_outflow_minor", outflow)

        # —— 通胀（若 analytics 已在采集 CPI，这里只读不算，避免口径分叉）——
        if self.policy is not None:
            inflation = self.policy.inflation_bps()
            if inflation is not None:
                put("bank_inflation_bps", int(inflation))
                put("inflation_gap_bps", int(inflation) - policy.target_inflation_bps)

        return count

    def cleanup(self, *, keep_days: int = 180) -> int:
        cutoff = now_ms() - int(keep_days) * DAY_MS
        return self.db.delete("metric_samples", "bucket_ms < ?", (cutoff,))
