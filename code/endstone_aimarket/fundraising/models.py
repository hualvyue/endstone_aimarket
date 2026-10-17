"""募资数据模型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FundraisingRecord:
    record_id: str
    company_account_id: str
    funder_account_id: str
    kind: str                      # invest / finance / sponsor
    amount_minor: int = 0
    units: int = 0                 # 投资=股数，融资=期限天数，赞助=广告天数
    ledger_tx_id: Optional[str] = None
    created_at_ms: int = 0
    funder_name: Optional[str] = None

    @property
    def kind_name(self) -> str:
        return {"invest": "投资", "finance": "融资", "sponsor": "赞助"}.get(self.kind, self.kind)

    @classmethod
    def from_row(cls, row: dict) -> "FundraisingRecord":
        return cls(
            record_id=row["record_id"],
            company_account_id=row["company_account_id"],
            funder_account_id=row["funder_account_id"],
            kind=row["kind"],
            amount_minor=int(row.get("amount_minor", 0)),
            units=int(row.get("units", 0)),
            ledger_tx_id=row.get("ledger_tx_id"),
            created_at_ms=int(row.get("created_at_ms", 0)),
            funder_name=row.get("funder_name"),
        )


@dataclass
class Sponsorship:
    sponsorship_id: str
    company_account_id: str
    sponsor_account_id: str
    amount_minor: int = 0
    display_name: Optional[str] = None
    starts_at_ms: int = 0
    expires_at_ms: int = 0
    status: str = "active"

    def is_active(self) -> bool:
        import time

        return self.status == "active" and self.expires_at_ms > int(time.time() * 1000)

    @classmethod
    def from_row(cls, row: dict) -> "Sponsorship":
        return cls(
            sponsorship_id=row["sponsorship_id"],
            company_account_id=row["company_account_id"],
            sponsor_account_id=row["sponsor_account_id"],
            amount_minor=int(row.get("amount_minor", 0)),
            display_name=row.get("display_name"),
            starts_at_ms=int(row.get("starts_at_ms", 0)),
            expires_at_ms=int(row.get("expires_at_ms", 0)),
            status=row.get("status", "active"),
        )
