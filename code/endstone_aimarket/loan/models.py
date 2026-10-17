"""贷款数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.constants import LoanStatus

DAY_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000


@dataclass
class Loan:
    loan_id: str
    borrower_account_id: str
    is_company: bool = False
    principal_minor: int = 0
    outstanding_principal_minor: int = 0
    accrued_interest_minor: int = 0
    paid_interest_minor: int = 0
    rate_bps_per_day: int = 0
    term_days: int = 7
    disbursed_at_ms: int = 0
    due_at_ms: int = 0
    grace_until_ms: int = 0
    status: str = LoanStatus.ACTIVE.value
    guarantee_account_id: Optional[str] = None
    credit_limit_minor: int = 0
    last_accrual_ms: int = 0
    reason: Optional[str] = None
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0
    # 债权归属：None/'bank' = 央行放款（还款即回收货币）；
    # 'player' = 玩家出资的融资（还款要真的付给那位玩家）
    lender_account_id: Optional[str] = None
    lender_kind: str = "bank"
    # 信贷产品种类：loan / overdraft / card / mortgage / discount。
    # 央行各产品共用这张表，靠 product 区分；产品特有元数据放各自表里。
    product: str = "loan"
    # 利息起算时刻。0 = 从 disbursed_at_ms 起算；信用卡设成还款日 → 免息期。
    interest_start_ms: int = 0

    @property
    def is_player_lent(self) -> bool:
        return self.lender_kind == "player" and bool(self.lender_account_id)

    @property
    def is_credit_product(self) -> bool:
        """是否属于央行的信贷产品（透支/信用卡/按揭/再贴现）。"""
        return self.product in ("overdraft", "card", "mortgage", "discount")

    def interest_from_ms(self) -> int:
        """实际开始计息的时刻。"""
        if self.interest_start_ms > 0:
            return self.interest_start_ms
        return self.disbursed_at_ms

    @property
    def total_due_minor(self) -> int:
        return self.outstanding_principal_minor + self.accrued_interest_minor

    @property
    def is_open(self) -> bool:
        return self.status in (
            LoanStatus.ACTIVE.value,
            LoanStatus.OVERDUE.value,
            LoanStatus.COLLECTING.value,
        )

    @property
    def is_collecting(self) -> bool:
        return self.status == LoanStatus.COLLECTING.value

    def overdue_ms(self, now: Optional[int] = None) -> int:
        import time
        current = int(now if now is not None else time.time() * 1000)
        return max(0, current - self.due_at_ms)

    @classmethod
    def from_row(cls, row: dict) -> "Loan":
        return cls(
            loan_id=row["loan_id"],
            borrower_account_id=row["borrower_account_id"],
            is_company=bool(row.get("is_company", 0)),
            principal_minor=int(row.get("principal_minor", 0)),
            outstanding_principal_minor=int(row.get("outstanding_principal_minor", 0)),
            accrued_interest_minor=int(row.get("accrued_interest_minor", 0)),
            paid_interest_minor=int(row.get("paid_interest_minor", 0)),
            rate_bps_per_day=int(row.get("rate_bps_per_day", 0)),
            term_days=int(row.get("term_days", 0)),
            disbursed_at_ms=int(row.get("disbursed_at_ms", 0)),
            due_at_ms=int(row.get("due_at_ms", 0)),
            grace_until_ms=int(row.get("grace_until_ms", 0)),
            status=row.get("status", LoanStatus.ACTIVE.value),
            guarantee_account_id=row.get("guarantee_account_id"),
            credit_limit_minor=int(row.get("credit_limit_minor", 0)),
            last_accrual_ms=int(row.get("last_accrual_ms", 0)),
            reason=row.get("reason"),
            version=int(row.get("version", 0)),
            created_at_ms=int(row.get("created_at_ms", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
            lender_account_id=row.get("lender_account_id"),
            lender_kind=row.get("lender_kind") or "bank",
            product=row.get("product") or "loan",
            interest_start_ms=int(row.get("interest_start_ms", 0) or 0),
        )


@dataclass
class RepossessedItem:
    item_uid: str
    loan_id: str
    item_json: str
    qty: int
    est_value_minor: int = 0
    status: str = "stored"      # stored / sold / returned
    sold_price_minor: Optional[int] = None

    @classmethod
    def from_row(cls, row: dict) -> "RepossessedItem":
        return cls(
            item_uid=row["item_uid"],
            loan_id=row["loan_id"],
            item_json=row["item_json"],
            qty=int(row["qty"]),
            est_value_minor=int(row.get("est_value_minor", 0)),
            status=row.get("status", "stored"),
            sold_price_minor=row.get("sold_price_minor"),
        )


@dataclass
class SeizeReport:
    """一次追债的结果。"""

    loan_id: str = ""
    borrower_account_id: str = ""
    items_taken: int = 0
    items_sold: int = 0
    items_stored: int = 0          # 没有官方定价、暂不出售、留待归还的物品
    proceeds_minor: int = 0
    repaid_minor: int = 0
    refunded_minor: int = 0
    remaining_debt_minor: int = 0
    settled: bool = False
    messages: List[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"没收 {self.items_taken} 件"]
        if self.items_sold:
            parts.append(f"变卖 {self.items_sold} 件得 {self.proceeds_minor}")
        if self.items_stored:
            parts.append(f"暂存 {self.items_stored} 件（无官方定价）")
        parts.append(f"抵债 {self.repaid_minor}")
        if self.refunded_minor:
            parts.append(f"退还 {self.refunded_minor}")
        parts.append(f"剩余欠款 {self.remaining_debt_minor}")
        return "，".join(parts)
