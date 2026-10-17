"""股票数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.constants import ShareStatus


@dataclass
class ShareListing:
    company_account_id: str
    authorized_shares: int = 0
    circulating_shares: int = 0
    ipo_price_minor: int = 0
    last_price_minor: int = 0
    nav_minor: int = 0
    nav_per_share_minor: int = 0
    rand_bps: int = 0
    price_version: int = 0
    status: str = ShareStatus.LISTED.value
    listed_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def is_tradable(self) -> bool:
        return self.status == ShareStatus.LISTED.value

    @property
    def market_cap_minor(self) -> int:
        return self.last_price_minor * self.circulating_shares

    @classmethod
    def from_row(cls, row: dict) -> "ShareListing":
        return cls(
            company_account_id=row["company_account_id"],
            authorized_shares=int(row.get("authorized_shares", 0)),
            circulating_shares=int(row.get("circulating_shares", 0)),
            ipo_price_minor=int(row.get("ipo_price_minor", 0)),
            last_price_minor=int(row.get("last_price_minor", 0)),
            nav_minor=int(row.get("nav_minor", 0)),
            nav_per_share_minor=int(row.get("nav_per_share_minor", 0)),
            rand_bps=int(row.get("rand_bps", 0)),
            price_version=int(row.get("price_version", 0)),
            status=row.get("status", ShareStatus.LISTED.value),
            listed_at_ms=int(row.get("listed_at_ms", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
        )


@dataclass
class Holding:
    company_account_id: str
    holder_account_id: str
    shares: int = 0
    avg_cost_minor: int = 0
    updated_at_ms: int = 0
    holder_name: Optional[str] = None
    company_name: Optional[str] = None
    last_price_minor: int = 0

    @property
    def market_value_minor(self) -> int:
        return self.shares * self.last_price_minor

    @property
    def cost_basis_minor(self) -> int:
        return self.shares * self.avg_cost_minor

    @property
    def profit_minor(self) -> int:
        return self.market_value_minor - self.cost_basis_minor

    @classmethod
    def from_row(cls, row: dict) -> "Holding":
        return cls(
            company_account_id=row["company_account_id"],
            holder_account_id=row["holder_account_id"],
            shares=int(row.get("shares", 0)),
            avg_cost_minor=int(row.get("avg_cost_minor", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
            holder_name=row.get("holder_name"),
            company_name=row.get("company_name"),
            last_price_minor=int(row.get("last_price_minor", 0) or 0),
        )


@dataclass
class Quote:
    """一次报价。UI 展示与下单结算共用同一份，避免价格不一致。"""

    company_account_id: str
    price_minor: int
    nav_per_share_minor: int
    nav_minor: int
    circulating_shares: int
    rand_bps: int = 0
    price_version: int = 0
    status: str = ShareStatus.LISTED.value

    @property
    def is_tradable(self) -> bool:
        return self.status == ShareStatus.LISTED.value and self.price_minor > 0


@dataclass
class OrderResult:
    ok: bool = False
    side: str = "issue"
    shares: int = 0
    unit_price_minor: int = 0
    amount_minor: int = 0
    tax_minor: int = 0
    new_holding: int = 0
    message: str = ""


@dataclass
class DividendResult:
    ok: bool = False
    total_minor: int = 0
    per_share_minor: int = 0
    shareholder_count: int = 0
    tax_minor: int = 0
    message: str = ""
