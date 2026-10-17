"""货币政策工具箱。

央行的政策分两类：

* **自动**（每小时跑）：只看通胀偏离，用简化泰勒规则调基准利率。带**死区 +
  步长上限 + 冷却时间**三重防震荡；被行长锁定的项自动跳过。
* **手动**（行长执行）：公开市场操作、量化宽松/紧缩、汇率干预。

**公开市场操作**是央行吞吐货币的主要手段：买入物资 = 印钞投放，卖出物资 = 回收销毁。
它同时就是"汇率干预（金对实物购买力）"的执行器 —— 两者是同一次操作的两个视角：
公开市场操作看的是货币量，汇率干预看的是物价（CPI）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import OmoSide, PolicyLock, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.money import format_amount
from ..core.result import Result
from ..economy.service import EconomyService
from .models import OmoRecord
from .repository import DAY_MS, HOUR_MS, now_ms
from .service import EVENT_OMO_BUY, EVENT_OMO_SELL, EVENT_POLICY, EVENT_QE, CentralBankService

# CPI 基准：10000 = 币值不变
CPI_BASE = 10000
# 利率上限：3%/天。再高就不是经济调控而是抢劫了
MAX_RATE_BPS_PER_DAY = 300


class PolicyEngine:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        bank: CentralBankService,
        *,
        inventory=None,
        pricing=None,
        metrics_query=None,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.bank = bank
        self.inventory = inventory
        self.pricing = pricing
        self.metrics_query = metrics_query
        self.config = config
        self.log = log
        self.repo = bank.repo

    def _cfg_int(self, path: str, default: int) -> int:
        if self.config is None:
            return default
        return int(self.config.as_int(path, default) or default)

    # ==================== 指标读取 ====================

    def inflation_bps(self, lookback: int = 24) -> Optional[int]:
        """通胀率（基点）。由 CPI 序列现算，与监管报表同源。

        注意 `MetricsQuery.inflation_bps` 是关键字参数 —— 传位置参数会抛异常，
        被这里的 except 吞掉后表现为"永远没有数据"，自动调节就此静默失效。
        """
        if self.metrics_query is None:
            return None
        try:
            return int(self.metrics_query.inflation_bps(lookback=lookback))
        except Exception as e:
            if self.log:
                self.log.debug(f"读取通胀率失败: {e}")
            return None

    def fx_index_bps(self) -> int:
        """币值指数（金对实物的购买力）。10000 = 基准。

        直接用 CPI：价格上涨 = 币值下跌。这就是"汇率"在本系统里的含义 ——
        没有外汇，汇率就是金币对实物的购买力。
        """
        if self.metrics_query is None:
            return CPI_BASE
        try:
            value = self.metrics_query.latest_int("cpi_bps")
            return int(value) if value else CPI_BASE
        except Exception:
            return CPI_BASE

    # ==================== 公开市场操作 ====================

    def omo_spread_bps(self) -> int:
        """央行买卖的价差。

        央行**买得比商店便宜、卖得比商店贵** —— 否则玩家会在商店与央行之间套利，
        或者干脆绕开商店只跟央行交易，把商店经济架空。
        """
        return max(0, self._cfg_int("bank.omo_spread_bps", 500))

    def omo_unit_price(self, item_key: str, side: str) -> int:
        """成交单价。以官方指导价为基准，按方向施加价差。"""
        base = 0
        if self.pricing is not None:
            base = int(self.pricing.quote_price(item_key, "buy") or 0)
        if base <= 0:
            return 0
        spread = self.omo_spread_bps()
        if side == OmoSide.BUY.value:
            return max(1, base * (10000 - spread) // 10000)
        return max(1, base * (10000 + spread) // 10000)

    def omo_max_per_op_minor(self) -> int:
        """单次操作上限。默认取 M0 的 1%，避免一键把经济砸穿。"""
        fixed = self.bank.policy().omo_max_per_op_minor
        if fixed > 0:
            return fixed
        return max(1, self.bank.m0() * 100 // 10000)

    def _omo_guard(self, amount: int, *, is_qe: bool) -> Result[None]:
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if is_qe:
            remaining = self.qe_remaining_minor()
            if amount > remaining:
                return Result.failure(
                    ErrorCode.SUPPLY_LIMIT,
                    f"超出量化宽松总额度，剩 {format_amount(remaining)}"
                    f"（封顶为 M0 的 {self.bank.policy().qe_cap_bps / 100:.0f}%）",
                )
            return Result.success(None)
        cap = self.omo_max_per_op_minor()
        if amount > cap:
            return Result.failure(
                ErrorCode.SUPPLY_LIMIT,
                f"单次公开市场操作上限 {format_amount(cap)}。"
                f"要更大规模请用 /bank qe（量化宽松）",
            )
        return Result.success(None)

    # ---------- QE 额度 ----------

    def qe_cap_minor(self) -> int:
        policy = self.bank.policy()
        return max(0, self.bank.m0() * max(0, policy.qe_cap_bps) // 10000)

    def qe_remaining_minor(self) -> int:
        return max(0, self.qe_cap_minor() - self.bank.policy().qe_used_minor)

    def qe_cooldown_ok(self) -> Result[None]:
        hours = max(0, self._cfg_int("bank.qe_cooldown_hours", 6))
        if hours <= 0:
            return Result.success(None)
        last = self.bank.repo.recent_events(1, kind=EVENT_QE)
        if not last:
            return Result.success(None)
        elapsed = now_ms() - int(last[0]["created_at_ms"])
        if elapsed < hours * HOUR_MS:
            remain = (hours * HOUR_MS - elapsed) // HOUR_MS + 1
            return Result.failure(
                ErrorCode.BANK_COOLDOWN, f"上一次量化操作才过去不久，请等约 {remain} 小时"
            )
        return Result.success(None)

    # ---------- 买入：投放货币 ----------

    def omo_buy(
        self,
        *,
        account_id: str,
        item_key: str,
        qty: int,
        player=None,
        item_info: Optional[Dict[str, Any]] = None,
        is_qe: bool = False,
    ) -> Result[OmoRecord]:
        """央行向市场买入实物：投放货币、增加储备。"""
        qty = int(qty)
        if qty <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        unit = self.omo_unit_price(item_key, OmoSide.BUY.value)
        if unit <= 0:
            return Result.failure(
                ErrorCode.PRICE_NOT_SET, f"{item_key} 没有官方指导价，无法进行公开市场操作"
            )
        amount = unit * qty
        guard = self._omo_guard(amount, is_qe=is_qe)
        if not guard.ok:
            return guard

        info = dict(item_info or {})
        info["type"] = item_key
        info.setdefault("count", 1)
        info.setdefault("data", 0)

        # 背包操作不可回滚，所以**先收货、再动账**；账务失败就把货还回去。
        taken_qty = qty
        if player is not None and self.inventory is not None:
            taken = self.inventory.take(player, info, qty)
            if not taken.ok or int(taken.value or 0) < qty:
                taken_qty = int(taken.value or 0)
                if taken_qty > 0:
                    self.inventory.give(player, info, taken_qty)  # 不做部分成交，原样退回
                return Result.failure(
                    ErrorCode.ITEM_NOT_ENOUGH, f"对方没有足够的 {item_key}"
                )

        m0_before = self.bank.m0()
        try:
            with self.db.transaction():
                paid = self.economy.deposit(
                    account_id, amount, reason="omo_buy", idem=f"omo-buy:{item_key}:{now_ms()}"
                )
                if not paid.ok:
                    raise AimarketError(paid.code, paid.message)
                self.repo.adjust_reserve(
                    item_key, qty_delta=taken_qty, value_delta=amount, item_name=item_key
                )
                op_id = self.repo.record_omo(
                    side=OmoSide.BUY.value,
                    item_key=item_key,
                    qty=taken_qty,
                    unit_minor=unit,
                    amount_minor=amount,
                    price_bps=-self.omo_spread_bps(),
                    is_qe=is_qe,
                    actor_account_id=account_id,
                    m0_before_minor=m0_before,
                    m0_after_minor=m0_before,
                    detail="量化宽松买入" if is_qe else "公开市场买入",
                )
                if is_qe:
                    policy = self.bank.policy()
                    policy.qe_used_minor += amount
                    self.repo.save_policy(policy)
        except AimarketError as e:
            if player is not None and self.inventory is not None:
                self.inventory.give(player, info, taken_qty)  # 账务失败，货退回
            return Result.failure(e.code, e.message)

        self.bank.record_flow(
            EVENT_QE if is_qe else EVENT_OMO_BUY, amount,
            detail=f"买入 {item_key}×{taken_qty}",
        )
        if self.log:
            self.log.info(
                f"[央行]{'量化宽松' if is_qe else '公开市场买入'} {item_key}×{taken_qty}，"
                f"投放 {format_amount(amount)}"
            )
        record = self.repo.recent_omo(1)
        return Result.success(record[0] if record else None)

    # ---------- 卖出：回收货币 ----------

    def omo_sell(
        self,
        *,
        account_id: str,
        item_key: str,
        qty: int,
        player=None,
        item_info: Optional[Dict[str, Any]] = None,
        is_qe: bool = False,
    ) -> Result[OmoRecord]:
        """央行向市场卖出储备：回收货币、减少储备。"""
        qty = int(qty)
        if qty <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        reserve = self.repo.get_reserve(item_key)
        if reserve is None or reserve.qty < qty:
            have = reserve.qty if reserve else 0
            return Result.failure(
                ErrorCode.BANK_RESERVE_EMPTY,
                f"央行储备不足：{item_key} 只有 {have} 个，无法卖出 {qty} 个",
            )
        unit = self.omo_unit_price(item_key, OmoSide.SELL.value)
        if unit <= 0:
            return Result.failure(
                ErrorCode.PRICE_NOT_SET, f"{item_key} 没有官方指导价，无法进行公开市场操作"
            )
        amount = unit * qty
        guard = self._omo_guard(amount, is_qe=is_qe)
        if not guard.ok:
            return guard

        info = dict(item_info or {})
        info["type"] = item_key
        info.setdefault("count", 1)
        info.setdefault("data", 0)

        # 先确认买家付得起、背包也放得下（放不下就别收钱）
        buyer = self.economy.repo.get_account(account_id)
        if buyer is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if buyer.available_minor < amount:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"买家可用余额不足，需要 {format_amount(amount)}",
            )
        if player is not None and self.inventory is not None:
            capacity = self.inventory.capacity_for(player, info)
            if int(capacity or 0) < qty:
                return Result.failure(
                    ErrorCode.INVENTORY_FULL, f"对方背包放不下 {qty} 个 {item_key}"
                )

        m0_before = self.bank.m0()
        cost = reserve.unit_cost_minor * qty
        try:
            with self.db.transaction():
                burned = self.economy.withdraw(
                    account_id, amount, reason="omo_sell", idem=f"omo-sell:{item_key}:{now_ms()}"
                )
                if not burned.ok:
                    raise AimarketError(burned.code, burned.message)
                self.repo.adjust_reserve(item_key, qty_delta=-qty, value_delta=-cost)
                self.repo.record_omo(
                    side=OmoSide.SELL.value,
                    item_key=item_key,
                    qty=qty,
                    unit_minor=unit,
                    amount_minor=amount,
                    price_bps=self.omo_spread_bps(),
                    is_qe=is_qe,
                    actor_account_id=account_id,
                    m0_before_minor=m0_before,
                    m0_after_minor=m0_before,
                    detail="量化紧缩卖出" if is_qe else "公开市场卖出",
                )
                if is_qe:
                    policy = self.bank.policy()
                    policy.qe_used_minor = max(0, policy.qe_used_minor - amount)
                    self.repo.save_policy(policy)
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        if player is not None and self.inventory is not None:
            given = self.inventory.give(player, info, qty)
            if not given.ok or int(given.value or 0) < qty:
                # 发货不足：把收到的钱退回去（钱货必须一致）
                self.economy.deposit(
                    account_id, amount, reason="omo_sell_refund",
                    idem=f"omo-refund:{item_key}:{now_ms()}",
                )
                return Result.failure(ErrorCode.INVENTORY_FULL, "背包空间不足，已退款")

        self.bank.record_flow(
            EVENT_QE if is_qe else EVENT_OMO_SELL, amount,
            detail=f"卖出 {item_key}×{qty}",
        )
        if self.log:
            self.log.info(
                f"[央行]{'量化紧缩' if is_qe else '公开市场卖出'} {item_key}×{qty}，"
                f"回收 {format_amount(amount)}"
            )
        record = self.repo.recent_omo(1)
        return Result.success(record[0] if record else None)

    # ==================== 自动调节 ====================

    def auto_adjust(self, *, now: Optional[int] = None) -> Dict[str, Any]:
        """简化泰勒规则：通胀偏离目标就动基准利率。

        三重防震荡：**死区**（偏离在容忍带内不动）、**步长上限**（单次最多调多少）、
        **冷却时间**（两次调整之间必须隔多久）。被行长锁定的项一律跳过。
        """
        policy = self.bank.policy()
        if not self.bank.enabled():
            return {"skipped": "bank_disabled"}
        if not policy.auto_policy:
            return {"skipped": "auto_disabled"}

        current = int(now if now is not None else now_ms())
        if policy.last_adjust_ms and current - policy.last_adjust_ms < policy.adjust_cooldown_ms:
            return {"skipped": "cooldown"}

        inflation = self.inflation_bps()
        if inflation is None:
            return {"skipped": "no_data"}

        gap = inflation - policy.target_inflation_bps
        if abs(gap) <= policy.target_band_bps:
            return {"skipped": "in_band", "inflation_bps": inflation, "gap_bps": gap}

        changed: Dict[str, Any] = {}
        if not policy.is_locked(PolicyLock.BASE_RATE.value):
            step = gap // 10
            step = max(-policy.max_step_bps, min(policy.max_step_bps, step))
            if step:
                new_rate = max(0, min(MAX_RATE_BPS_PER_DAY, policy.base_rate_bps_per_day + step))
                if new_rate != policy.base_rate_bps_per_day:
                    policy.base_rate_bps_per_day = new_rate
                    changed["base_rate_bps_per_day"] = new_rate
                    changed["step_bps"] = step

        if not changed:
            return {"inflation_bps": inflation, "gap_bps": gap, "changed": {}}

        policy.last_adjust_ms = current
        self.bank.save_policy(policy)
        detail = (
            f"通胀 {inflation / 100:.2f}% 偏离目标 {policy.target_inflation_bps / 100:.2f}%，"
            f"基准利率调至 {policy.base_rate_bps_per_day / 100:.2f}%/天"
        )
        self.repo.add_event(kind=EVENT_POLICY, detail=detail, m0_after_minor=self.bank.m0())
        if self.log:
            self.log.info(f"[央行自动调节] {detail}")
        return {
            "inflation_bps": inflation,
            "gap_bps": gap,
            "changed": changed,
            "detail": detail,
        }

    # ==================== 汇率干预建议 ====================

    def fx_signal(self) -> Optional[Dict[str, Any]]:
        """币值偏离信号。给行长/告警用：该买还是该卖。

        买入储备 = 投放货币 = 抗通缩；卖出储备 = 回收货币 = 抗通胀。
        """
        policy = self.bank.policy()
        index = self.fx_index_bps()
        if index <= 0:
            return None
        # 币值 = 10000 / CPI 的相对变化；这里直接用 CPI 判断物价高低
        hi = CPI_BASE + policy.target_band_bps * 10
        lo = CPI_BASE - policy.target_band_bps * 10
        if index > hi:
            return {
                "action": "sell",
                "reason": f"物价指数 {index / 100:.2f}% 高于基准，币值走弱",
                "suggest": "卖出储备回收货币",
            }
        if index < lo:
            return {
                "action": "buy",
                "reason": f"物价指数 {index / 100:.2f}% 低于基准，币值走强",
                "suggest": "买入储备投放货币",
            }
        return None

    # ==================== 报表 ====================

    def describe(self) -> str:
        policy = self.bank.policy()
        lines = [policy.describe(), ""]
        headroom = self.bank.credit_headroom_minor(policy)
        lines.append(
            f"准备金闸门：可放贷上限 {format_amount(self.bank.reserve_cap_minor(policy))}"
            f"，已放 {format_amount(self.bank.credit_total_minor())}，"
            f"还能放 {format_amount(headroom)}"
        )
        lines.append(f"货币乘数 {self.bank.balance_sheet().multiplier_bps / 10000:.2f}")
        inflation = self.inflation_bps()
        if inflation is not None:
            lines.append(f"当前通胀 {inflation / 100:.2f}% ｜ 币值指数 {self.fx_index_bps() / 100:.2f}%")
        lines.append(
            f"量化宽松额度：已用 {format_amount(policy.qe_used_minor)} / "
            f"封顶 {format_amount(self.qe_cap_minor())}"
        )
        return "\n".join(lines)
