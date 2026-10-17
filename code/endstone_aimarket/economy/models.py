"""经济内核的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..core.constants import AccountKind, AccountStatus, TxKind, TxStatus


@dataclass
class Account:
    account_id: str
    kind: str = AccountKind.PLAYER.value
    xuid: Optional[str] = None
    player_uuid: Optional[str] = None
    current_name: Optional[str] = None
    balance_minor: int = 0
    held_minor: int = 0
    status: str = AccountStatus.ACTIVE.value
    version: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def available_minor(self) -> int:
        """可动用余额 = 总余额 - 冻结部分。"""
        return self.balance_minor - self.held_minor

    @property
    def is_usable(self) -> bool:
        return self.status == AccountStatus.ACTIVE.value

    @classmethod
    def from_row(cls, row: dict) -> "Account":
        return cls(
            account_id=row["account_id"],
            kind=row.get("kind", AccountKind.PLAYER.value),
            xuid=row.get("xuid"),
            player_uuid=row.get("player_uuid"),
            current_name=row.get("current_name"),
            balance_minor=int(row.get("balance_minor", 0)),
            held_minor=int(row.get("held_minor", 0)),
            status=row.get("status", AccountStatus.ACTIVE.value),
            version=int(row.get("version", 0)),
            created_at_ms=int(row.get("created_at_ms", 0)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
        )


@dataclass
class Balance:
    """余额快照。available 才是玩家真正能花的钱。"""

    account_id: str
    balance_minor: int = 0
    held_minor: int = 0

    @property
    def available_minor(self) -> int:
        return self.balance_minor - self.held_minor


@dataclass
class Entry:
    """一条账本分录。同一 ledger_tx 下所有分录的 delta_minor 之和必须为 0。"""

    account_id: str
    role: str
    delta_minor: int
    balance_after_minor: int = 0


@dataclass
class Receipt:
    """操作回执：调用方应把它展示给玩家或写进审计。"""

    tx_id: str
    kind: str = TxKind.TRANSFER.value
    status: str = TxStatus.FILLED.value
    gross_minor: int = 0
    tax_minor: int = 0
    net_minor: int = 0
    entries: list = field(default_factory=list)
    created_at_ms: int = 0
    idempotent_replay: bool = False  # True 表示这次是幂等重放，未真正扣款


@dataclass
class TxRecord:
    """账本流水查询返回。"""

    tx_id: str
    kind: str
    status: str
    role: str
    delta_minor: int
    balance_after_minor: int
    correlation_id: Optional[str]
    metadata_json: Optional[str]
    created_at_ms: int
    counterparty: Optional[str] = None


# 分录角色
ROLE_PAYER = "payer"
ROLE_PAYEE = "payee"
ROLE_TAX = "tax"
ROLE_SOURCE = "source"
ROLE_SINK = "sink"
ROLE_ESCROW = "escrow"
ROLE_ACTOR = "actor"
ROLE_OWNER = "owner"
