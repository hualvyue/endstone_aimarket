"""中央银行的数据模型。

这一层只描述数据形态与派生口径，不做任何落库与业务判定。

**央行是"三家合一"的主体**（发行 + 财政 + 储备），但账上仍然是分开的科目：
- `sys:mint`（kind=system，可为负）= 发行科目，负余额就是基础货币净投放
- `sys:treasury`（kind=treasury，禁负）= 财政科目，收税与回收货币
- `bank_reserves` = 实物储备科目，公开市场操作吞吐的物资

`BalanceSheet` 把它们合成**一张可读的资产负债表** —— 这是全部货币政策的数据基础。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.money import format_amount

# ==================== 取值助手（sqlite3.Row 与 dict 都吃） ====================


def _g(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _i(row: Any, key: str, default: int = 0) -> int:
    value = _g(row, key, None)
    return default if value is None else int(value)


def _s(row: Any, key: str, default: str = "") -> str:
    value = _g(row, key, None)
    return default if value is None else str(value)


# ==================== 行长 ====================


@dataclass
class Governor:
    """央行行长。只认 xuid：改名不丢权限，离线不影响判定。"""

    account_id: str
    xuid: str
    display_name: Optional[str] = None
    granted_at_ms: int = 0
    revoked_at_ms: Optional[int] = None
    note: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at_ms is None

    @classmethod
    def from_row(cls, row: Any) -> "Governor":
        return cls(
            account_id=_s(row, "account_id"),
            xuid=_s(row, "xuid"),
            display_name=_g(row, "display_name"),
            granted_at_ms=_i(row, "granted_at_ms"),
            revoked_at_ms=_g(row, "revoked_at_ms"),
            note=_g(row, "note"),
        )


# ==================== 货币政策状态 ====================


@dataclass
class PolicyState:
    """央行的政策状态。所有产品利率都由 `base_rate` 加利差派生。

    行长只需调一个基准利率，全局联动 —— 这是"你只调一个数"的实现方式。
    """

    base_rate_bps_per_day: int = 30        # 基准利率（日，基点，30 = 0.30%/天）
    rrr_bps: int = 1000                    # 法定存款准备金率（1000 = 10%）
    discount_wedge_bps: int = 50           # 再贴现加价
    demand_spread_bps: int = -15           # 活期利差（负 = 活期利率低于基准）
    term_premium_bps: int = 20             # 定期加成
    credit_spread_bps: int = 20            # 贷款利差
    od_premium_bps: int = 40               # 透支利差
    card_premium_bps: int = 60             # 信用卡利差
    target_inflation_bps: int = 200        # 通胀目标
    target_band_bps: int = 100             # 容忍带
    auto_policy: bool = True               # 自动调节开关
    max_step_bps: int = 10                 # 单次调整步长上限
    adjust_cooldown_ms: int = 3_600_000    # 调整冷却
    omo_max_per_op_minor: int = 0          # 单次公开市场操作上限（0 = 按 M0 比例推算）
    qe_cap_bps: int = 2000                 # QE 总量封顶（占 M0 比例）
    qe_used_minor: int = 0                 # 已用 QE 额度
    reserve_floor_car_bps: int = 1000       # 资产覆盖率红线（1000 = 10%）
    locked: List[str] = field(default_factory=list)   # 行长锁定的项，自动调节跳过
    last_adjust_ms: int = 0
    updated_at_ms: int = 0

    # ---------- 利率派生：全部由基准利率 + 利差算出 ----------

    def demand_rate_bps(self) -> int:
        return max(0, self.base_rate_bps_per_day + self.demand_spread_bps)

    def time_rate_bps(self) -> int:
        return max(0, self.base_rate_bps_per_day + self.term_premium_bps)

    def credit_rate_bps(self) -> int:
        return max(0, self.base_rate_bps_per_day + self.credit_spread_bps)

    def overdraft_rate_bps(self) -> int:
        return max(0, self.base_rate_bps_per_day + self.od_premium_bps)

    def card_rate_bps(self) -> int:
        return max(0, self.base_rate_bps_per_day + self.card_premium_bps)

    def discount_rate_bps(self) -> int:
        """再贴现率 = 基准 + 加价。永远高于基准，否则会变成套利通道。"""
        return max(0, self.base_rate_bps_per_day + self.discount_wedge_bps)

    # ---------- 锁定判定 ----------

    def is_locked(self, key: str) -> bool:
        return key in self.locked

    def lock(self, key: str) -> None:
        if key not in self.locked:
            self.locked.append(key)

    def unlock(self, key: str) -> None:
        self.locked = [k for k in self.locked if k != key]

    # ---------- 通胀目标区间 ----------

    @property
    def inflation_floor_bps(self) -> int:
        return self.target_inflation_bps - self.target_band_bps

    @property
    def inflation_ceil_bps(self) -> int:
        return self.target_inflation_bps + self.target_band_bps

    def describe(self) -> str:
        locked = "、".join(self.locked) if self.locked else "无"
        return (
            f"基准利率 {self.base_rate_bps_per_day / 100:.2f}%/天 ｜ "
            f"准备金率 {self.rrr_bps / 100:.2f}%\n"
            f"活期 {self.demand_rate_bps() / 100:.2f}% ｜ 定期 {self.time_rate_bps() / 100:.2f}% ｜ "
            f"贷款 {self.credit_rate_bps() / 100:.2f}% ｜ 透支 {self.overdraft_rate_bps() / 100:.2f}% ｜ "
            f"信用卡 {self.card_rate_bps() / 100:.2f}% ｜ 再贴现 {self.discount_rate_bps() / 100:.2f}%\n"
            f"通胀目标 {self.target_inflation_bps / 100:.2f}% ± {self.target_band_bps / 100:.2f}%"
            f"（{(self.inflation_floor_bps) / 100:.2f}% ~ {self.inflation_ceil_bps / 100:.2f}%）\n"
            f"自动调节 {'开启' if self.auto_policy else '关闭'} ｜ 被行长锁定 {locked}"
        )

    @classmethod
    def from_row(cls, row: Any) -> "PolicyState":
        import json

        locked: List[str] = []
        raw = _g(row, "locked_json")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    locked = [str(x) for x in parsed]
            except Exception:
                locked = []
        return cls(
            base_rate_bps_per_day=_i(row, "base_rate_bps_per_day", 30),
            rrr_bps=_i(row, "rrr_bps", 1000),
            discount_wedge_bps=_i(row, "discount_wedge_bps", 50),
            demand_spread_bps=_i(row, "demand_spread_bps", -15),
            term_premium_bps=_i(row, "term_premium_bps", 20),
            credit_spread_bps=_i(row, "credit_spread_bps", 20),
            od_premium_bps=_i(row, "od_premium_bps", 40),
            card_premium_bps=_i(row, "card_premium_bps", 60),
            target_inflation_bps=_i(row, "target_inflation_bps", 200),
            target_band_bps=_i(row, "target_band_bps", 100),
            auto_policy=bool(_i(row, "auto_policy", 1)),
            max_step_bps=_i(row, "max_step_bps", 10),
            adjust_cooldown_ms=_i(row, "adjust_cooldown_ms", 3_600_000),
            omo_max_per_op_minor=_i(row, "omo_max_per_op_minor", 0),
            qe_cap_bps=_i(row, "qe_cap_bps", 2000),
            qe_used_minor=_i(row, "qe_used_minor", 0),
            reserve_floor_car_bps=_i(row, "reserve_floor_car_bps", 1000),
            locked=locked,
            last_adjust_ms=_i(row, "last_adjust_ms", 0),
            updated_at_ms=_i(row, "updated_at_ms", 0),
        )


# ==================== 存款 ====================


@dataclass
class Deposit:
    """一笔存款。钱实际躺在 `deposit_account_id` 那个账户里。"""

    deposit_id: str
    holder_account_id: str
    deposit_account_id: str
    kind: str = "demand"                     # demand / time / wealth
    principal_minor: int = 0
    rate_bps_per_day: int = 0
    locked_until_ms: int = 0
    accrued_minor: int = 0
    paid_interest_minor: int = 0
    last_accrual_ms: int = 0
    product_id: Optional[str] = None
    status: str = "active"
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def is_fixed(self) -> bool:
        """是否是锁定期产品（定期 / 理财）。"""
        return self.kind in ("time", "wealth")

    @property
    def total_minor(self) -> int:
        return self.principal_minor + self.accrued_minor

    def is_locked(self, now_ms: int) -> bool:
        return self.is_fixed and self.locked_until_ms > now_ms

    def remaining_days(self, now_ms: int) -> int:
        if not self.is_fixed:
            return 0
        left = self.locked_until_ms - now_ms
        return max(0, left // 86_400_000)

    def describe(self) -> str:
        kind_name = {"demand": "活期", "time": "定期", "wealth": "理财"}.get(self.kind, self.kind)
        text = (
            f"{kind_name} 本金 {format_amount(self.principal_minor)}"
            f" 累计利息 {format_amount(self.accrued_minor)}"
            f"（{self.rate_bps_per_day / 100:.2f}%/天）"
        )
        if self.is_fixed and self.locked_until_ms:
            text += f" 到期 {self.locked_until_ms // 1000}"
        return text

    @classmethod
    def from_row(cls, row: Any) -> "Deposit":
        return cls(
            deposit_id=_s(row, "deposit_id"),
            holder_account_id=_s(row, "holder_account_id"),
            deposit_account_id=_s(row, "deposit_account_id"),
            kind=_s(row, "kind", "demand"),
            principal_minor=_i(row, "principal_minor"),
            rate_bps_per_day=_i(row, "rate_bps_per_day"),
            locked_until_ms=_i(row, "locked_until_ms"),
            accrued_minor=_i(row, "accrued_minor"),
            paid_interest_minor=_i(row, "paid_interest_minor"),
            last_accrual_ms=_i(row, "last_accrual_ms"),
            product_id=_g(row, "product_id"),
            status=_s(row, "status", "active"),
            version=_i(row, "version"),
            created_at_ms=_i(row, "created_at_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


# ==================== 授信（透支） ====================


@dataclass
class CreditLine:
    """透支授信额度。

    **透支不把余额改成负数**（数据库有 `CHECK (balance_minor >= 0 OR kind='system')`），
    而是"额度 + 自动放款"：付款时余额不足就从额度里借一笔补上。
    玩家感受到的是"能花超"，物理上账户余额恒为正。
    """

    line_id: str
    holder_account_id: str
    credit_limit_minor: int = 0
    used_minor: int = 0
    rate_bps_per_day: int = 0
    due_at_ms: int = 0
    autopay: bool = True
    status: str = "active"
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def available_minor(self) -> int:
        return max(0, self.credit_limit_minor - self.used_minor)

    def describe(self) -> str:
        return (
            f"额度 {format_amount(self.credit_limit_minor)} ｜ "
            f"已用 {format_amount(self.used_minor)} ｜ "
            f"可用 {format_amount(self.available_minor)} ｜ "
            f"{self.rate_bps_per_day / 100:.2f}%/天"
        )

    @classmethod
    def from_row(cls, row: Any) -> "CreditLine":
        return cls(
            line_id=_s(row, "line_id"),
            holder_account_id=_s(row, "holder_account_id"),
            credit_limit_minor=_i(row, "credit_limit_minor"),
            used_minor=_i(row, "used_minor"),
            rate_bps_per_day=_i(row, "rate_bps_per_day"),
            due_at_ms=_i(row, "due_at_ms"),
            autopay=bool(_i(row, "autopay", 1)),
            status=_s(row, "status", "active"),
            version=_i(row, "version"),
            created_at_ms=_i(row, "created_at_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


# ==================== 信用卡 ====================


@dataclass
class Card:
    """信用卡。与透支的区别：有账单周期与免息期、有最低还款额、额度更大。"""

    card_id: str
    holder_account_id: str
    credit_limit_minor: int = 0
    used_minor: int = 0
    apr_bps_per_day: int = 0
    penalty_bps_per_day: int = 0
    cycle_days: int = 7
    min_pay_bps: int = 1000
    grace_days: int = 0
    next_statement_ms: int = 0
    status: str = "active"
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def available_minor(self) -> int:
        return max(0, self.credit_limit_minor - self.used_minor)

    def describe(self) -> str:
        return (
            f"额度 {format_amount(self.credit_limit_minor)} ｜ "
            f"已用 {format_amount(self.used_minor)} ｜ "
            f"可用 {format_amount(self.available_minor)} ｜ "
            f"{self.apr_bps_per_day / 100:.2f}%/天 ｜ 账单周期 {self.cycle_days} 天"
        )

    @classmethod
    def from_row(cls, row: Any) -> "Card":
        return cls(
            card_id=_s(row, "card_id"),
            holder_account_id=_s(row, "holder_account_id"),
            credit_limit_minor=_i(row, "credit_limit_minor"),
            used_minor=_i(row, "used_minor"),
            apr_bps_per_day=_i(row, "apr_bps_per_day"),
            penalty_bps_per_day=_i(row, "penalty_bps_per_day"),
            cycle_days=_i(row, "cycle_days", 7),
            min_pay_bps=_i(row, "min_pay_bps", 1000),
            grace_days=_i(row, "grace_days"),
            next_statement_ms=_i(row, "next_statement_ms"),
            status=_s(row, "status", "active"),
            version=_i(row, "version"),
            created_at_ms=_i(row, "created_at_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


@dataclass
class Statement:
    """信用卡账单。"""

    statement_id: str
    card_id: str
    period_end_ms: int = 0
    due_at_ms: int = 0
    amount_minor: int = 0
    min_pay_minor: int = 0
    paid_minor: int = 0
    penalty_minor: int = 0
    status: str = "open"
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def remaining_minor(self) -> int:
        return max(0, self.amount_minor - self.paid_minor)

    @classmethod
    def from_row(cls, row: Any) -> "Statement":
        return cls(
            statement_id=_s(row, "statement_id"),
            card_id=_s(row, "card_id"),
            period_end_ms=_i(row, "period_end_ms"),
            due_at_ms=_i(row, "due_at_ms"),
            amount_minor=_i(row, "amount_minor"),
            min_pay_minor=_i(row, "min_pay_minor"),
            paid_minor=_i(row, "paid_minor"),
            penalty_minor=_i(row, "penalty_minor"),
            status=_s(row, "status", "open"),
            created_at_ms=_i(row, "created_at_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


# ==================== 按揭 ====================


@dataclass
class Mortgage:
    """按揭：以实体店等资产作抵押的分期贷款。"""

    mortgage_id: str
    borrower_account_id: str
    loan_id: Optional[str] = None
    collateral_kind: str = "cash"
    collateral_ref: str = ""
    collateral_value_minor: int = 0
    principal_minor: int = 0
    outstanding_minor: int = 0
    rate_bps_per_day: int = 0
    term_days: int = 0
    periods: int = 1
    next_due_ms: int = 0
    status: str = "active"
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def describe(self) -> str:
        return (
            f"本金 {format_amount(self.principal_minor)} ｜ "
            f"剩余 {format_amount(self.outstanding_minor)} ｜ "
            f"{self.rate_bps_per_day / 100:.2f}%/天 ｜ {self.periods} 期"
        )

    @classmethod
    def from_row(cls, row: Any) -> "Mortgage":
        return cls(
            mortgage_id=_s(row, "mortgage_id"),
            borrower_account_id=_s(row, "borrower_account_id"),
            loan_id=_g(row, "loan_id"),
            collateral_kind=_s(row, "collateral_kind", "cash"),
            collateral_ref=_s(row, "collateral_ref"),
            collateral_value_minor=_i(row, "collateral_value_minor"),
            principal_minor=_i(row, "principal_minor"),
            outstanding_minor=_i(row, "outstanding_minor"),
            rate_bps_per_day=_i(row, "rate_bps_per_day"),
            term_days=_i(row, "term_days"),
            periods=_i(row, "periods", 1),
            next_due_ms=_i(row, "next_due_ms"),
            status=_s(row, "status", "active"),
            version=_i(row, "version"),
            created_at_ms=_i(row, "created_at_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


@dataclass
class MortgageSchedule:
    mortgage_id: str
    period_index: int
    due_at_ms: int = 0
    principal_minor: int = 0
    interest_minor: int = 0
    paid_minor: int = 0
    status: str = "pending"

    @property
    def due_total_minor(self) -> int:
        return self.principal_minor + self.interest_minor

    @property
    def remaining_minor(self) -> int:
        return max(0, self.due_total_minor - self.paid_minor)

    @classmethod
    def from_row(cls, row: Any) -> "MortgageSchedule":
        return cls(
            mortgage_id=_s(row, "mortgage_id"),
            period_index=_i(row, "period_index"),
            due_at_ms=_i(row, "due_at_ms"),
            principal_minor=_i(row, "principal_minor"),
            interest_minor=_i(row, "interest_minor"),
            paid_minor=_i(row, "paid_minor"),
            status=_s(row, "status", "pending"),
        )


# ==================== 实物储备与公开市场操作 ====================


@dataclass
class Reserve:
    """央行的实物储备。公开市场操作吞吐的就是它。"""

    item_key: str
    item_name: Optional[str] = None
    qty: int = 0
    book_value_minor: int = 0
    last_op_ms: int = 0
    updated_at_ms: int = 0

    @property
    def unit_cost_minor(self) -> int:
        """账面单位成本。用于卖出时结转成本。"""
        if self.qty <= 0:
            return 0
        return self.book_value_minor // self.qty

    def describe(self) -> str:
        return (
            f"{self.item_name or self.item_key} × {self.qty}"
            f"（账面 {format_amount(self.book_value_minor)}"
            f"，单价 {format_amount(self.unit_cost_minor)}）"
        )

    @classmethod
    def from_row(cls, row: Any) -> "Reserve":
        return cls(
            item_key=_s(row, "item_key"),
            item_name=_g(row, "item_name"),
            qty=_i(row, "qty"),
            book_value_minor=_i(row, "book_value_minor"),
            last_op_ms=_i(row, "last_op_ms"),
            updated_at_ms=_i(row, "updated_at_ms"),
        )


@dataclass
class OmoRecord:
    """一笔公开市场操作。"""

    op_id: str
    side: str                       # buy / sell
    item_key: str
    qty: int = 0
    unit_minor: int = 0
    amount_minor: int = 0
    price_bps: int = 0
    is_qe: bool = False
    actor_account_id: Optional[str] = None
    ledger_tx_id: Optional[str] = None
    m0_before_minor: int = 0
    m0_after_minor: int = 0
    detail: Optional[str] = None
    created_at_ms: int = 0

    def describe(self) -> str:
        word = "买入" if self.side == "buy" else "卖出"
        flow = "投放" if self.side == "buy" else "回收"
        return (
            f"{word} {self.item_key} × {self.qty}，单价 {format_amount(self.unit_minor)}，"
            f"合计 {format_amount(self.amount_minor)}（{flow}货币）"
        )

    @classmethod
    def from_row(cls, row: Any) -> "OmoRecord":
        return cls(
            op_id=_s(row, "op_id"),
            side=_s(row, "side"),
            item_key=_s(row, "item_key"),
            qty=_i(row, "qty"),
            unit_minor=_i(row, "unit_minor"),
            amount_minor=_i(row, "amount_minor"),
            price_bps=_i(row, "price_bps"),
            is_qe=bool(_i(row, "is_qe")),
            actor_account_id=_g(row, "actor_account_id"),
            ledger_tx_id=_g(row, "ledger_tx_id"),
            m0_before_minor=_i(row, "m0_before_minor"),
            m0_after_minor=_i(row, "m0_after_minor"),
            detail=_g(row, "detail"),
            created_at_ms=_i(row, "created_at_ms"),
        )


@dataclass
class Alert:
    alert_id: str
    kind: str
    severity: str = "warning"
    title: str = ""
    detail: Optional[str] = None
    metrics_json: Optional[str] = None
    acknowledged: bool = False
    created_at_ms: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "Alert":
        return cls(
            alert_id=_s(row, "alert_id"),
            kind=_s(row, "kind"),
            severity=_s(row, "severity", "warning"),
            title=_s(row, "title"),
            detail=_g(row, "detail"),
            metrics_json=_g(row, "metrics_json"),
            acknowledged=bool(_i(row, "acknowledged")),
            created_at_ms=_i(row, "created_at_ms"),
        )


# ==================== 资产负债表 ====================


@dataclass
class BalanceSheet:
    """央行的资产负债表快照，以及全套货币口径。

    这张表是货币政策的仪表盘：行长所有决策都基于它。
    """

    # ---------- 资产端 ----------
    reserves_minor: int = 0              # 实物储备（账面成本）
    loans_outstanding_minor: int = 0     # 对外放贷债权（含公司/个人贷款）
    overdraft_used_minor: int = 0        # 透支占用
    card_used_minor: int = 0             # 信用卡占用
    mortgage_outstanding_minor: int = 0  # 按揭剩余

    # ---------- 负债端 ----------
    base_money_minor: int = 0            # 基础货币 = -balance(sys:mint)
    demand_deposits_minor: int = 0       # 活期存款
    time_deposits_minor: int = 0         # 定期存款
    wealth_deposits_minor: int = 0       # 理财资金
    treasury_minor: int = 0              # 财政科目余额
    accrued_interest_minor: int = 0      # 已计提未付的存款利息

    # ---------- 流通口径 ----------
    m0_minor: int = 0                    # 玩家 + 公司手上的现金
    escrow_minor: int = 0                # 商店托管

    # ---------- 其他 ----------
    deposit_count: int = 0
    account_count: int = 0

    @property
    def deposit_total_minor(self) -> int:
        return self.demand_deposits_minor + self.time_deposits_minor + self.wealth_deposits_minor

    @property
    def credit_total_minor(self) -> int:
        return (
            self.loans_outstanding_minor
            + self.overdraft_used_minor
            + self.card_used_minor
            + self.mortgage_outstanding_minor
        )

    @property
    def asset_minor(self) -> int:
        """资产 = 实物储备 + 各类债权。"""
        return self.reserves_minor + self.credit_total_minor

    @property
    def liability_minor(self) -> int:
        """货币负债 = 基础货币（央行累计净投放），**只有这一项**。

        推导：恒等式 `基础货币 = M0 + 商店托管 + 财政余额 + 各类存款` —— 也就是说，
        所有非央行账户手里的钱，追根溯源都来自央行投放。存款只是这同一笔钱在不同
        主体之间的**分布**，绝不能再单独算一次负债，否则就是重复计算。

        由此资产负债表自动自洽：
          印钞放贷    → 负债 +100，债权 +100 → 资产不变
          买入储备    → 负债 +100，储备 +100 → 资产不变
          卖出储备    → 负债 −100，储备 −100 → 资产不变
          发存款利息  → 负债 +10，资产不变
          坏账核销    → 资产 −100，负债不变
        """
        return self.base_money_minor

    @property
    def coverable_liability_minor(self) -> int:
        """需要资产去覆盖的负债：存款 + 财政余额。

        **刻意不含基础货币** —— 流通中现金是央行的永续负债（不需要、也不可能
        "偿还"），把它算进覆盖范围，任何一次正常放贷都会显示成天量资不抵债，
        指标就彻底失去信息量了。
        """
        return self.deposit_total_minor + self.treasury_minor

    @property
    def capital_minor(self) -> int:
        """央行资本 = 资产 − 需覆盖负债。

        为负说明"央行靠印钞兑付玩家的存款"，这是真实的警示信号
        （正常状态下资产应当能覆盖存款）。
        """
        return self.asset_minor - self.coverable_liability_minor

    @property
    def m1_minor(self) -> int:
        return self.m0_minor + self.demand_deposits_minor

    @property
    def m2_minor(self) -> int:
        return self.m1_minor + self.time_deposits_minor + self.wealth_deposits_minor

    @property
    def multiplier_bps(self) -> int:
        """货币乘数 = M1 / M0。无商行时接近 10000（即 1.00 倍）。"""
        if self.m0_minor <= 0:
            return 0
        return self.m1_minor * 10000 // self.m0_minor

    @property
    def rwa_minor(self) -> int:
        """风险加权资产。不同科目权重不同，抵押类更高。"""
        return (
            self.loans_outstanding_minor * 10000 // 10000
            + self.overdraft_used_minor * 10000 // 10000
            + self.card_used_minor * 12000 // 10000
            + self.mortgage_outstanding_minor * 14000 // 10000
            + self.reserves_minor * 5000 // 10000
        )

    @property
    def car_bps(self) -> int:
        """资本充足率（口径：资产覆盖率）。

        在"央行直营存款 + 能印钞"的模型里，传统资本充足率**没有严格对应物** ——
        初始投放的货币天然没有资产支撑（它由国家的发行信用背书）。所以这里定义为：

            资产 ÷ 需覆盖负债（玩家存款 + 财政余额）

        含义：**央行用自有资产覆盖了多少比例的对外负债**。
          100%   资产完全覆盖存款（最健康）
          < 100% 差额靠发行信用支撑（这是常态，不是危机）

        它衡量的是"扩表相对存款的激进程度"，越低保值压力越大。
        """
        liability = self.coverable_liability_minor
        if liability <= 0:
            return 99999  # 还没有对外负债，视为充足
        return self.asset_minor * 10000 // liability

    def describe(self, *, money=format_amount) -> str:
        return (
            f"── 央行资产 {money(self.asset_minor)} ──\n"
            f"  实物储备 {money(self.reserves_minor)}\n"
            f"  放贷债权 {money(self.loans_outstanding_minor)}\n"
            f"  透支占用 {money(self.overdraft_used_minor)}\n"
            f"  信用卡占用 {money(self.card_used_minor)}\n"
            f"  按揭剩余 {money(self.mortgage_outstanding_minor)}\n"
            f"── 央行负债·基础货币 {money(self.liability_minor)} ──\n"
            f"  流通现金 M0 {money(self.m0_minor)}\n"
            f"  商店托管 {money(self.escrow_minor)}\n"
            f"  财政余额 {money(self.treasury_minor)}\n"
            f"  活期存款 {money(self.demand_deposits_minor)}\n"
            f"  定期存款 {money(self.time_deposits_minor)}\n"
            f"  理财资金 {money(self.wealth_deposits_minor)}\n"
            f"  应计利息 {money(self.accrued_interest_minor)}\n"
            f"── 资产覆盖率 {self.car_bps / 100:.2f}%"
            f"（资产 ÷ 存款+财政）｜ 净差额 {money(self.capital_minor)} ──"
        )

    def money_supply_text(self, *, money=format_amount) -> str:
        return (
            f"M0 {money(self.m0_minor)} ｜ M1 {money(self.m1_minor)} ｜ M2 {money(self.m2_minor)}\n"
            f"货币乘数 {self.multiplier_bps / 10000:.2f} ｜ 存款总额 {money(self.deposit_total_minor)} ｜ "
            f"放贷总额 {money(self.credit_total_minor)}"
        )
