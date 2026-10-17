"""公司数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core.constants import CompanyRole, CompanyStatus


@dataclass
class Company:
    business_id: str
    account_id: str
    name: str
    founder_account_id: str
    registered_capital_minor: int = 0
    turnover_tax_bps: int = 0
    dividend_bps: int = 0
    reserve_minor: int = 0
    status: str = CompanyStatus.ACTIVE.value
    created_at_ms: int = 0
    updated_at_ms: int = 0
    # 募资（投资/融资/赞助）
    finance_rate_bps_per_day: int = 100
    finance_term_days: int = 30
    sponsor_price_per_day_minor: int = 0
    fundraising_locked_until_ms: int = 0
    last_fundraise_ms: int = 0
    total_raised_minor: int = 0

    @property
    def is_active(self) -> bool:
        return self.status == CompanyStatus.ACTIVE.value

    @property
    def is_fundraising_locked(self) -> bool:
        """募资锁定期内禁止给发起人分红（防"募完就跑"）。"""
        import time

        return self.fundraising_locked_until_ms > int(time.time() * 1000)

    @property
    def is_listed(self) -> bool:
        return self.status == CompanyStatus.ACTIVE.value

    @classmethod
    def from_row(cls, row: dict) -> "Company":
        return cls(
            business_id=row["business_id"],
            account_id=row["account_id"],
            name=row["name"],
            founder_account_id=row["founder_account_id"],
            registered_capital_minor=int(row.get("registered_capital_minor", 0)),
            turnover_tax_bps=int(row.get("turnover_tax_bps", 0)),
            dividend_bps=int(row.get("dividend_bps", 0)),
            reserve_minor=int(row.get("reserve_minor", 0)),
            status=row.get("status", CompanyStatus.ACTIVE.value),
            created_at_ms=int(row.get("created_at_ms", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
            finance_rate_bps_per_day=int(row.get("finance_rate_bps_per_day", 100) or 0),
            finance_term_days=int(row.get("finance_term_days", 30) or 30),
            sponsor_price_per_day_minor=int(row.get("sponsor_price_per_day_minor", 0) or 0),
            fundraising_locked_until_ms=int(row.get("fundraising_locked_until_ms", 0) or 0),
            last_fundraise_ms=int(row.get("last_fundraise_ms", 0) or 0),
            total_raised_minor=int(row.get("total_raised_minor", 0) or 0),
        )


@dataclass
class Member:
    business_id: str
    account_id: str
    role: str = CompanyRole.SHAREHOLDER.value
    joined_at_ms: int = 0
    name: Optional[str] = None
    shares: int = 0

    @property
    def is_founder(self) -> bool:
        return self.role == CompanyRole.FOUNDER.value


@dataclass
class AssetBreakdown:
    """公司资产拆解。股价与破产清算都用这一份口径，避免两处算法不一致。"""

    cash_minor: int = 0                 # 公司账户余额
    escrow_minor: int = 0               # 名下收购店的托管余额
    stock_value_minor: int = 0          # 旗下商店的货物价值（按官方回收价）
    collected_value_minor: int = 0      # 旗下商店收到的货价值
    debt_minor: int = 0                 # 未偿本金 + 应计利息
    shop_count: int = 0
    unpriced_items: int = 0             # 没有官方定价、按 0 计价的物品条目数

    @property
    def gross_minor(self) -> int:
        return (
            self.cash_minor
            + self.escrow_minor
            + self.stock_value_minor
            + self.collected_value_minor
        )

    @property
    def nav_minor(self) -> int:
        """净资产 = 总资产 − 负债。可以为负（资不抵债）。"""
        return self.gross_minor - self.debt_minor

    @property
    def is_insolvent(self) -> bool:
        return self.nav_minor < 0

    def describe(self) -> str:
        return (
            f"现金 {self.cash_minor} + 托管 {self.escrow_minor} "
            f"+ 库存 {self.stock_value_minor} + 收货 {self.collected_value_minor} "
            f"− 负债 {self.debt_minor} = 净资产 {self.nav_minor}"
        )


@dataclass
class LiquidationPlan:
    """破产清算方案：先算清楚，再执行。"""

    company_id: str
    gross_minor: int = 0
    debt_minor: int = 0
    # 清偿顺序：[(顺位名, 应还金额, 实付金额)]
    payouts: List[tuple] = field(default_factory=list)
    to_shareholders_minor: int = 0
    # 无法清偿、由系统核销的缺口
    writeoff_minor: int = 0
    items_sold: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def total_paid(self) -> int:
        return sum(p[2] for p in self.payouts)

    def describe(self) -> str:
        lines = [f"清算总资产 {self.gross_minor}，负债 {self.debt_minor}"]
        for name, due, paid in self.payouts:
            if due == 0 and paid == 0:
                continue
            lines.append(f"  {name}: 应还 {due} / 实付 {paid}")
        lines.append(f"  股东分配: {self.to_shareholders_minor}")
        if self.writeoff_minor:
            lines.append(f"  系统核销缺口: {self.writeoff_minor}（由系统承担，不涉及股东个人）")
        return "\n".join(lines)
