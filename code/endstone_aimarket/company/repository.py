"""公司的持久化。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core.constants import CompanyRole, CompanyStatus
from ..core.database import Database
from ..core.ids import new_uuid7, normalize_name
from .models import Company, Member


def now_ms() -> int:
    return int(time.time() * 1000)


class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 建与查 ====================

    def name_taken(self, name: str) -> bool:
        norm = normalize_name(name)
        if not norm:
            return True
        return self.db.query_one(
            "SELECT business_id FROM companies WHERE name_norm = ?", (norm,)
        ) is not None

    def create(
        self,
        *,
        account_id: str,
        name: str,
        founder_account_id: str,
        registered_capital_minor: int,
        turnover_tax_bps: int = 0,
        dividend_bps: int = 0,
        reserve_minor: int = 0,
    ) -> Optional[Company]:
        business_id = new_uuid7()
        ts = now_ms()
        self.db.insert(
            "companies",
            {
                "business_id": business_id,
                "account_id": account_id,
                "name": name,
                "name_norm": normalize_name(name),
                "founder_account_id": founder_account_id,
                "registered_capital_minor": int(registered_capital_minor),
                "turnover_tax_bps": int(turnover_tax_bps),
                "dividend_bps": int(dividend_bps),
                "reserve_minor": int(reserve_minor),
                "status": CompanyStatus.ACTIVE.value,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        self.add_member(business_id, founder_account_id, CompanyRole.FOUNDER.value)
        return self.get(business_id)

    def get(self, business_id: str) -> Optional[Company]:
        row = self.db.query_one("SELECT * FROM companies WHERE business_id = ?", (business_id,))
        return Company.from_row(row) if row else None

    def get_by_name(self, name: str) -> Optional[Company]:
        norm = normalize_name(name)
        if not norm:
            return None
        row = self.db.query_one("SELECT * FROM companies WHERE name_norm = ?", (norm,))
        return Company.from_row(row) if row else None

    def get_by_account(self, account_id: str) -> Optional[Company]:
        row = self.db.query_one("SELECT * FROM companies WHERE account_id = ?", (account_id,))
        return Company.from_row(row) if row else None

    def list_all(self, *, active_only: bool = True, limit: int = 200) -> List[Company]:
        if active_only:
            rows = self.db.query_all(
                "SELECT * FROM companies WHERE status = ? ORDER BY created_at_ms LIMIT ?",
                (CompanyStatus.ACTIVE.value, int(limit)),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM companies ORDER BY created_at_ms LIMIT ?", (int(limit),)
            )
        return [Company.from_row(r) for r in rows]

    def list_by_member(self, account_id: str) -> List[Company]:
        rows = self.db.query_all(
            """SELECT c.* FROM companies c
                 JOIN company_members m ON m.business_id = c.business_id
                WHERE m.account_id = ? ORDER BY c.created_at_ms""",
            (account_id,),
        )
        return [Company.from_row(r) for r in rows]

    def set_status(self, business_id: str, status: str) -> int:
        return self.db.update(
            "companies",
            {"status": status, "updated_at_ms": now_ms()},
            "business_id = ?",
            (business_id,),
        )

    def update(self, business_id: str, data: Dict[str, Any]) -> int:
        payload = dict(data)
        payload["updated_at_ms"] = now_ms()
        return self.db.update("companies", payload, "business_id = ?", (business_id,))

    def delete(self, business_id: str) -> int:
        return self.db.delete("companies", "business_id = ?", (business_id,))

    def count_all(self, *, active_only: bool = True) -> int:
        if active_only:
            return int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM companies WHERE status = ?",
                    (CompanyStatus.ACTIVE.value,),
                    default=0,
                ) or 0
            )
        return int(self.db.scalar("SELECT COUNT(*) FROM companies", default=0) or 0)

    # ==================== 成员 ====================

    def add_member(self, business_id: str, account_id: str, role: str = CompanyRole.SHAREHOLDER.value) -> None:
        existing = self.db.query_one(
            "SELECT business_id FROM company_members WHERE business_id = ? AND account_id = ?",
            (business_id, account_id),
        )
        if existing:
            return
        self.db.insert(
            "company_members",
            {
                "business_id": business_id,
                "account_id": account_id,
                "role": role,
                "joined_at_ms": now_ms(),
            },
        )

    def remove_member(self, business_id: str, account_id: str) -> int:
        return self.db.delete(
            "company_members", "business_id = ? AND account_id = ?", (business_id, account_id)
        )

    def get_member(self, business_id: str, account_id: str) -> Optional[Member]:
        row = self.db.query_one(
            "SELECT * FROM company_members WHERE business_id = ? AND account_id = ?",
            (business_id, account_id),
        )
        if row is None:
            return None
        return Member(
            business_id=row["business_id"],
            account_id=row["account_id"],
            role=row.get("role", CompanyRole.SHAREHOLDER.value),
            joined_at_ms=int(row.get("joined_at_ms", 0)),
        )

    def is_founder(self, business_id: str, account_id: str) -> bool:
        row = self.db.query_one(
            "SELECT role FROM company_members WHERE business_id = ? AND account_id = ?",
            (business_id, account_id),
        )
        return bool(row and row.get("role") == CompanyRole.FOUNDER.value)

    def members(self, business_id: str) -> List[Member]:
        rows = self.db.query_all(
            """SELECT m.*, a.current_name
                 FROM company_members m
                 LEFT JOIN accounts a ON a.account_id = m.account_id
                WHERE m.business_id = ?
                ORDER BY CASE m.role WHEN 'founder' THEN 0 ELSE 1 END, m.joined_at_ms""",
            (business_id,),
        )
        out = []
        for r in rows:
            out.append(
                Member(
                    business_id=r["business_id"],
                    account_id=r["account_id"],
                    role=r.get("role", CompanyRole.SHAREHOLDER.value),
                    joined_at_ms=int(r.get("joined_at_ms", 0)),
                    name=r.get("current_name"),
                )
            )
        return out

    def member_count(self, business_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM company_members WHERE business_id = ?",
                (business_id,),
                default=0,
            ) or 0
        )

    # ==================== 营业额流水 ====================

    def record_turnover(
        self,
        *,
        business_id: str,
        gross_minor: int,
        tax_minor: int,
        shop_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> None:
        self.db.insert(
            "company_turnover",
            {
                "business_id": business_id,
                "shop_id": shop_id,
                "direction": direction,
                "gross_minor": int(gross_minor),
                "tax_minor": int(tax_minor),
                "occurred_at_ms": now_ms(),
            },
        )

    def turnover_between(self, business_id: str, *, since_ms: int, until_ms: Optional[int] = None) -> Dict[str, int]:
        sql = (
            "SELECT COALESCE(SUM(gross_minor),0) AS gross, COALESCE(SUM(tax_minor),0) AS tax, "
            "COUNT(*) AS n FROM company_turnover WHERE business_id = ? AND occurred_at_ms >= ?"
        )
        params = [business_id, int(since_ms)]
        if until_ms is not None:
            sql += " AND occurred_at_ms < ?"
            params.append(int(until_ms))
        row = self.db.query_one(sql, tuple(params)) or {}
        return {
            "gross_minor": int(row.get("gross") or 0),
            "tax_minor": int(row.get("tax") or 0),
            "count": int(row.get("n") or 0),
        }

    # ==================== 名下资产（给 NAV 用）====================

    def shops_of(self, account_id: str) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM shops WHERE owner_account_id = ?", (account_id,)
        )

    def outstanding_debt(self, account_id: str) -> int:
        """未偿本金 + 应计利息。这里直接查表，避免 company 反向依赖 loan 模块。"""
        row = self.db.query_one(
            """SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor), 0) AS debt
                 FROM loans
                WHERE borrower_account_id = ?
                  AND status IN ('active','overdue','collecting')""",
            (account_id,),
        )
        return int((row or {}).get("debt") or 0)
