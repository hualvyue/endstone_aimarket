"""股票持久化。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core.constants import ShareStatus
from ..core.database import Database
from ..core.ids import new_uuid7
from .models import Holding, ShareListing


def now_ms() -> int:
    return int(time.time() * 1000)


class StockRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 上市信息 ====================

    def get_listing(self, company_account_id: str) -> Optional[ShareListing]:
        row = self.db.query_one(
            "SELECT * FROM company_shares WHERE company_account_id = ?", (company_account_id,)
        )
        return ShareListing.from_row(row) if row else None

    def create_listing(
        self,
        *,
        company_account_id: str,
        authorized_shares: int,
        ipo_price_minor: int,
        initial_circulating: int = 0,
    ) -> Optional[ShareListing]:
        ts = now_ms()
        self.db.insert(
            "company_shares",
            {
                "company_account_id": company_account_id,
                "authorized_shares": int(authorized_shares),
                "circulating_shares": int(initial_circulating),
                "ipo_price_minor": int(ipo_price_minor),
                "last_price_minor": int(ipo_price_minor),
                "nav_minor": 0,
                "nav_per_share_minor": int(ipo_price_minor),
                "rand_bps": 0,
                "price_version": 0,
                "status": ShareStatus.LISTED.value,
                "listed_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_listing(company_account_id)

    def update_listing(self, company_account_id: str, data: Dict[str, Any]) -> int:
        payload = dict(data)
        payload["updated_at_ms"] = now_ms()
        return self.db.update(
            "company_shares", payload, "company_account_id = ?", (company_account_id,)
        )

    def listed_all(self, *, only_listed: bool = True, limit: int = 200) -> List[ShareListing]:
        if only_listed:
            rows = self.db.query_all(
                "SELECT * FROM company_shares WHERE status = ? ORDER BY last_price_minor DESC LIMIT ?",
                (ShareStatus.LISTED.value, int(limit)),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM company_shares ORDER BY updated_at_ms DESC LIMIT ?", (int(limit),)
            )
        return [ShareListing.from_row(r) for r in rows]

    def set_status(self, company_account_id: str, status: str) -> int:
        return self.update_listing(company_account_id, {"status": status})

    def count_listed(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM company_shares WHERE status = ?",
                (ShareStatus.LISTED.value,),
                default=0,
            ) or 0
        )

    # ==================== 持股 ====================

    def get_holding(self, company_account_id: str, holder_account_id: str) -> Optional[Holding]:
        row = self.db.query_one(
            """SELECT h.*, a.current_name AS holder_name, cs.last_price_minor
                 FROM share_holdings h
                 LEFT JOIN accounts a ON a.account_id = h.holder_account_id
                 LEFT JOIN company_shares cs ON cs.company_account_id = h.company_account_id
                WHERE h.company_account_id = ? AND h.holder_account_id = ?""",
            (company_account_id, holder_account_id),
        )
        return Holding.from_row(row) if row else None

    def upsert_holding(
        self, *, company_account_id: str, holder_account_id: str, shares: int, avg_cost_minor: int
    ) -> None:
        existing = self.db.query_one(
            "SELECT shares FROM share_holdings WHERE company_account_id = ? AND holder_account_id = ?",
            (company_account_id, holder_account_id),
        )
        if existing:
            self.db.update(
                "share_holdings",
                {"shares": int(shares), "avg_cost_minor": int(avg_cost_minor), "updated_at_ms": now_ms()},
                "company_account_id = ? AND holder_account_id = ?",
                (company_account_id, holder_account_id),
            )
        else:
            self.db.insert(
                "share_holdings",
                {
                    "company_account_id": company_account_id,
                    "holder_account_id": holder_account_id,
                    "shares": int(shares),
                    "avg_cost_minor": int(avg_cost_minor),
                    "updated_at_ms": now_ms(),
                },
            )

    def delete_holding(self, company_account_id: str, holder_account_id: str) -> int:
        return self.db.delete(
            "share_holdings",
            "company_account_id = ? AND holder_account_id = ?",
            (company_account_id, holder_account_id),
        )

    def holdings_of_holder(self, holder_account_id: str) -> List[Holding]:
        rows = self.db.query_all(
            """SELECT h.*, a.current_name AS holder_name, c.name AS company_name,
                      cs.last_price_minor
                 FROM share_holdings h
                 LEFT JOIN accounts a ON a.account_id = h.holder_account_id
                 LEFT JOIN company_shares cs ON cs.company_account_id = h.company_account_id
                 LEFT JOIN companies c ON c.account_id = h.company_account_id
                WHERE h.holder_account_id = ? AND h.shares > 0
                ORDER BY h.shares DESC""",
            (holder_account_id,),
        )
        return [Holding.from_row(r) for r in rows]

    def shareholders_of(self, company_account_id: str) -> List[Holding]:
        rows = self.db.query_all(
            """SELECT h.*, a.current_name AS holder_name, cs.last_price_minor
                 FROM share_holdings h
                 LEFT JOIN accounts a ON a.account_id = h.holder_account_id
                 LEFT JOIN company_shares cs ON cs.company_account_id = h.company_account_id
                WHERE h.company_account_id = ? AND h.shares > 0
                ORDER BY h.shares DESC""",
            (company_account_id,),
        )
        return [Holding.from_row(r) for r in rows]

    # ==================== 价格历史 ====================

    def record_price(
        self,
        *,
        company_account_id: str,
        bucket_ms: int,
        nav_minor: int,
        circulating_shares: int,
        nav_per_share_minor: int,
        rand_bps: int,
        price_minor: int,
    ) -> None:
        self.db.insert_or_ignore(
            "share_price_history",
            {
                "company_account_id": company_account_id,
                "bucket_ms": int(bucket_ms),
                "nav_minor": int(nav_minor),
                "circulating_shares": int(circulating_shares),
                "nav_per_share_minor": int(nav_per_share_minor),
                "rand_bps": int(rand_bps),
                "price_minor": int(price_minor),
            },
        )

    def price_series(self, company_account_id: str, *, limit: int = 168) -> List[Dict[str, Any]]:
        return self.db.query_all(
            """SELECT * FROM share_price_history WHERE company_account_id = ?
                ORDER BY bucket_ms DESC LIMIT ?""",
            (company_account_id, int(limit)),
        )

    # ==================== 交易流水 ====================

    def add_order(
        self,
        *,
        company_account_id: str,
        holder_account_id: str,
        side: str,
        shares: int,
        unit_price_minor: int,
        amount_minor: int,
        ledger_tx_id: Optional[str] = None,
    ) -> str:
        order_id = new_uuid7()
        self.db.insert(
            "share_orders",
            {
                "order_id": order_id,
                "company_account_id": company_account_id,
                "holder_account_id": holder_account_id,
                "side": side,
                "shares": int(shares),
                "unit_price_minor": int(unit_price_minor),
                "amount_minor": int(amount_minor),
                "ledger_tx_id": ledger_tx_id,
                "created_at_ms": now_ms(),
            },
        )
        return order_id

    def orders_of_holder(self, holder_account_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM share_orders WHERE holder_account_id = ? ORDER BY created_at_ms DESC LIMIT ?",
            (holder_account_id, int(limit)),
        )

    # ==================== 分红 ====================

    def find_dividend(self, idem: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM dividend_records WHERE idem = ?", (idem,))

    def add_dividend(
        self,
        *,
        company_account_id: str,
        per_share_minor: int,
        total_minor: int,
        shareholder_count: int,
        tax_minor: int,
        idem: str,
    ) -> str:
        dividend_id = new_uuid7()
        self.db.insert(
            "dividend_records",
            {
                "dividend_id": dividend_id,
                "company_account_id": company_account_id,
                "per_share_minor": int(per_share_minor),
                "total_minor": int(total_minor),
                "shareholder_count": int(shareholder_count),
                "tax_minor": int(tax_minor),
                "idem": idem,
                "paid_at_ms": now_ms(),
            },
        )
        return dividend_id

    def dividends_of(self, company_account_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM dividend_records WHERE company_account_id = ? ORDER BY paid_at_ms DESC LIMIT ?",
            (company_account_id, int(limit)),
        )

    # ==================== 统计 ====================

    def total_market_cap(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(last_price_minor * circulating_shares), 0) "
            "FROM company_shares WHERE status = ?",
            (ShareStatus.LISTED.value,),
            default=0,
        )
        return int(value or 0)
