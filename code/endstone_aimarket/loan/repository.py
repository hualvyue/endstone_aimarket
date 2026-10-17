"""贷款与追债的持久化。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core.constants import LoanStatus
from ..core.database import Database
from ..core.ids import new_uuid7
from .models import HOUR_MS, Loan, RepossessedItem


def now_ms() -> int:
    return int(time.time() * 1000)


class LoanRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 贷款 ====================

    def create(
        self,
        *,
        borrower_account_id: str,
        principal_minor: int,
        rate_bps_per_day: int,
        term_days: int,
        is_company: bool = False,
        credit_limit_minor: int = 0,
        grace_days: int = 3,
        reason: Optional[str] = None,
        loan_id: Optional[str] = None,
        lender_account_id: Optional[str] = None,
        lender_kind: str = "bank",
        product: str = "loan",
        interest_start_ms: int = 0,
    ) -> Optional[Loan]:
        lid = loan_id or new_uuid7()
        ts = now_ms()
        due = ts + int(term_days) * 24 * HOUR_MS
        self.db.insert(
            "loans",
            {
                "loan_id": lid,
                "borrower_account_id": borrower_account_id,
                "is_company": 1 if is_company else 0,
                "principal_minor": int(principal_minor),
                "outstanding_principal_minor": int(principal_minor),
                "accrued_interest_minor": 0,
                "paid_interest_minor": 0,
                "rate_bps_per_day": int(rate_bps_per_day),
                "term_days": int(term_days),
                "disbursed_at_ms": ts,
                "due_at_ms": due,
                "grace_until_ms": due + int(grace_days) * 24 * HOUR_MS,
                "status": LoanStatus.ACTIVE.value,
                "guarantee_account_id": None,
                "credit_limit_minor": int(credit_limit_minor),
                "last_accrual_ms": ts,
                "reason": reason,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
                "lender_account_id": lender_account_id,
                "lender_kind": lender_kind,
                "product": product,
                "interest_start_ms": int(interest_start_ms),
            },
        )
        return self.get(lid)

    # ---------- 按产品查（央行信贷共用 loans 表） ----------

    def open_loans_of_product(self, borrower_account_id: str, product: str) -> List[Loan]:
        rows = self.db.query_all(
            "SELECT * FROM loans WHERE borrower_account_id = ? AND product = ? "
            "AND status IN ('active','overdue','collecting') ORDER BY created_at_ms",
            (borrower_account_id, product),
        )
        return [Loan.from_row(r) for r in rows]

    def outstanding_of_product(self, product: str, *, borrower_account_id: Optional[str] = None) -> int:
        """某产品的未偿本息合计。余额一律从这里派生，不做冗余字段，天然不会不同步。"""
        if borrower_account_id:
            value = self.db.scalar(
                "SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor), 0) "
                "FROM loans WHERE product = ? AND borrower_account_id = ? "
                "AND status IN ('active','overdue','collecting')",
                (product, borrower_account_id),
                default=0,
            )
        else:
            value = self.db.scalar(
                "SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor), 0) "
                "FROM loans WHERE product = ? AND status IN ('active','overdue','collecting')",
                (product,),
                default=0,
            )
        return int(value or 0)

    def outstanding_bank_total(self, *, exclude_products: Optional[List[str]] = None) -> int:
        """央行（银行口径）对外债权合计。玩家出资的融资不算在内。"""
        sql = (
            "SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor), 0) "
            "FROM loans WHERE lender_kind = 'bank' AND status IN ('active','overdue','collecting')"
        )
        params: List[Any] = []
        if exclude_products:
            marks = ",".join("?" for _ in exclude_products)
            sql += f" AND product NOT IN ({marks})"
            params.extend(exclude_products)
        value = self.db.scalar(sql, tuple(params), default=0)
        return int(value or 0)

    def credit_rows(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """央行信贷清单（透支/信用卡/按揭/再贴现），供监管报表使用。"""
        rows = self.db.query_all(
            """SELECT loan_id, borrower_account_id, product, principal_minor,
                      outstanding_principal_minor + accrued_interest_minor AS outstanding,
                      rate_bps_per_day, due_at_ms, status
                 FROM loans
                WHERE product != 'loan' AND status IN ('active','overdue','collecting')
                ORDER BY created_at_ms DESC LIMIT ?""",
            (int(limit),),
        )
        return [dict(r) for r in rows]

    def loans_lent_by(self, lender_account_id: str, *, open_only: bool = True) -> List[Loan]:
        """某位玩家出资的债权（融资）。"""
        if open_only:
            rows = self.db.query_all(
                """SELECT * FROM loans WHERE lender_account_id = ? AND lender_kind = 'player'
                    AND status IN ('active','overdue','collecting')
                    ORDER BY due_at_ms""",
                (lender_account_id,),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM loans WHERE lender_account_id = ? AND lender_kind = 'player' "
                "ORDER BY created_at_ms DESC",
                (lender_account_id,),
            )
        return [Loan.from_row(r) for r in rows]

    def debt_rows_with_lender(self, borrower_account_id: str) -> List[Dict[str, Any]]:
        """取一只债务清单，带债权人信息，供清算按顺位分配。"""
        rows = self.db.query_all(
            """SELECT loan_id, lender_account_id, lender_kind,
                      outstanding_principal_minor + accrued_interest_minor AS outstanding,
                      guarantee_account_id, is_company, due_at_ms
                 FROM loans
                WHERE borrower_account_id = ? AND status IN ('active','overdue','collecting')
                ORDER BY due_at_ms""",
            (borrower_account_id,),
        )
        return [dict(r) for r in rows]

    def get(self, loan_id: str) -> Optional[Loan]:
        row = self.db.query_one("SELECT * FROM loans WHERE loan_id = ?", (loan_id,))
        return Loan.from_row(row) if row else None

    def open_loans_of(self, account_id: str) -> List[Loan]:
        rows = self.db.query_all(
            """SELECT * FROM loans WHERE borrower_account_id = ?
                AND status IN ('active','overdue','collecting') ORDER BY due_at_ms""",
            (account_id,),
        )
        return [Loan.from_row(r) for r in rows]

    def total_debt_of(self, account_id: str) -> int:
        row = self.db.query_one(
            """SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor),0) AS debt
                 FROM loans WHERE borrower_account_id = ?
                  AND status IN ('active','overdue','collecting')""",
            (account_id,),
        )
        return int((row or {}).get("debt") or 0)

    def all_open(self, *, limit: int = 1000) -> List[Loan]:
        rows = self.db.query_all(
            """SELECT * FROM loans WHERE status IN ('active','overdue','collecting')
                ORDER BY due_at_ms LIMIT ?""",
            (int(limit),),
        )
        return [Loan.from_row(r) for r in rows]

    def collecting_loans(self) -> List[Loan]:
        rows = self.db.query_all(
            "SELECT * FROM loans WHERE status = ? ORDER BY due_at_ms",
            (LoanStatus.COLLECTING.value,),
        )
        return [Loan.from_row(r) for r in rows]

    def update(self, loan_id: str, data: Dict[str, Any]) -> int:
        payload = dict(data)
        payload["updated_at_ms"] = now_ms()
        return self.db.update("loans", payload, "loan_id = ?", (loan_id,))

    def set_status(self, loan_id: str, status: str) -> int:
        return self.update(loan_id, {"status": status})

    def stats(self) -> Dict[str, int]:
        row = self.db.query_one(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor),0) AS debt
                 FROM loans WHERE status IN ('active','overdue','collecting')"""
        ) or {}
        overdue = self.db.query_one(
            "SELECT COUNT(*) AS n FROM loans WHERE status IN ('overdue','collecting')"
        ) or {}
        settled = self.db.query_one(
            "SELECT COUNT(*) AS n FROM loans WHERE status = 'settled'"
        ) or {}
        written = self.db.query_one(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(principal_minor - outstanding_principal_minor),0) AS recovered
                 FROM loans WHERE status = 'written_off'"""
        ) or {}
        return {
            "open_count": int(row.get("n") or 0),
            "open_debt_minor": int(row.get("debt") or 0),
            "overdue_count": int(overdue.get("n") or 0),
            "settled_count": int(settled.get("n") or 0),
            "written_off_count": int(written.get("n") or 0),
            "written_off_recovered_minor": int(written.get("recovered") or 0),
        }

    # ==================== 还款 ====================

    def add_repayment(
        self,
        *,
        loan_id: str,
        amount_minor: int,
        principal_part_minor: int,
        interest_part_minor: int,
        ledger_tx_id: Optional[str] = None,
        source: str = "manual",
    ) -> str:
        repay_id = new_uuid7()
        self.db.insert(
            "loan_repayments",
            {
                "repay_id": repay_id,
                "loan_id": loan_id,
                "amount_minor": int(amount_minor),
                "principal_part_minor": int(principal_part_minor),
                "interest_part_minor": int(interest_part_minor),
                "ledger_tx_id": ledger_tx_id,
                "source": source,
                "paid_at_ms": now_ms(),
            },
        )
        return repay_id

    def repayments_of(self, loan_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM loan_repayments WHERE loan_id = ? ORDER BY paid_at_ms DESC LIMIT ?",
            (loan_id, int(limit)),
        )

    # ==================== 计息 ====================

    def accrue(
        self, *, loan_id: str, bucket_ms: int, interest_delta_minor: int
    ) -> bool:
        """按小时计提。返回 True 表示这一小时是首次计提（幂等保护）。"""
        if interest_delta_minor <= 0:
            return False
        row = self.db.query_one(
            "SELECT interest_delta_minor FROM loan_accruals WHERE loan_id = ? AND bucket_ms = ?",
            (loan_id, int(bucket_ms)),
        )
        if row is not None:
            return False
        self.db.insert(
            "loan_accruals",
            {
                "loan_id": loan_id,
                "bucket_ms": int(bucket_ms),
                "interest_delta_minor": int(interest_delta_minor),
                "created_at_ms": now_ms(),
            },
        )
        return True

    # ==================== 违约记录 ====================

    def add_default(
        self,
        *,
        loan_id: str,
        reason: str,
        trigger_kind: str,
        snapshot: Optional[dict] = None,
    ) -> str:
        import json

        default_id = new_uuid7()
        self.db.insert(
            "loan_defaults",
            {
                "default_id": default_id,
                "loan_id": loan_id,
                "reason": reason,
                "trigger_kind": trigger_kind,
                "snapshot_json": json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
                "created_at_ms": now_ms(),
            },
        )
        return default_id

    def defaults_of(self, loan_id: str) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM loan_defaults WHERE loan_id = ? ORDER BY created_at_ms",
            (loan_id,),
        )

    def has_default(self, loan_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT default_id FROM loan_defaults WHERE loan_id = ? LIMIT 1", (loan_id,)
            )
            is not None
        )

    # ==================== 被没收物品 ====================

    def store_repossessed(
        self,
        *,
        loan_id: str,
        item_json: str,
        qty: int,
        est_value_minor: int,
        status: str = "stored",
        sold_price_minor: Optional[int] = None,
    ) -> str:
        uid = new_uuid7()
        ts = now_ms()
        self.db.insert(
            "repossessed_items",
            {
                "item_uid": uid,
                "loan_id": loan_id,
                "item_json": item_json,
                "qty": int(qty),
                "est_value_minor": int(est_value_minor),
                "status": status,
                "sold_price_minor": sold_price_minor,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return uid

    def repossessed_of(self, loan_id: str, *, status: Optional[str] = None) -> List[RepossessedItem]:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM repossessed_items WHERE loan_id = ? AND status = ? ORDER BY created_at_ms",
                (loan_id, status),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM repossessed_items WHERE loan_id = ? ORDER BY created_at_ms",
                (loan_id,),
            )
        return [RepossessedItem.from_row(r) for r in rows]

    def mark_repossessed(self, item_uid: str, *, status: str, sold_price_minor: Optional[int] = None) -> int:
        return self.db.update(
            "repossessed_items",
            {
                "status": status,
                "sold_price_minor": sold_price_minor,
                "updated_at_ms": now_ms(),
            },
            "item_uid = ?",
            (item_uid,),
        )
