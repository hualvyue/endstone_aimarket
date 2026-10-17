"""账户与账本的持久化。

依赖：仅 Python 标准库 sqlite3，无任何第三方包。
所有写方法都假定调用方已经处在 Database.transaction() 之中，
因此这里不做任何 commit —— 原子性由上层统一控制。
"""
from __future__ import annotations

import time
from typing import List, Optional

from ..core.constants import AccountKind, AccountStatus, TxStatus
from ..core.database import Database
from ..core.ids import normalize_name
from .models import Account, TxRecord

# 固定系统账户的 ID（不绑定任何玩家）
SYSTEM_MINT_ACCOUNT_ID = "sys:mint"        # 铸币来源，允许负余额 = 已发行货币总量
TREASURY_ACCOUNT_ID = "sys:treasury"       # 国库，收税


def now_ms() -> int:
    return int(time.time() * 1000)


class EconomyRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 账户 ====================

    def get_account(self, account_id: str) -> Optional[Account]:
        row = self.db.query_one("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
        return Account.from_row(row) if row else None

    def get_by_xuid(self, xuid: str) -> Optional[Account]:
        if not xuid:
            return None
        row = self.db.query_one("SELECT * FROM accounts WHERE xuid = ?", (str(xuid),))
        return Account.from_row(row) if row else None

    def get_by_uuid(self, player_uuid: str) -> Optional[Account]:
        if not player_uuid:
            return None
        row = self.db.query_one("SELECT * FROM accounts WHERE player_uuid = ?", (str(player_uuid),))
        return Account.from_row(row) if row else None

    def get_by_name(self, name: str) -> Optional[Account]:
        """按当前名字查账户。ux_account_names_current 保证唯一，故至多一个。"""
        norm = normalize_name(name)
        if not norm:
            return None
        row = self.db.query_one(
            "SELECT * FROM accounts WHERE account_id = "
            "(SELECT account_id FROM account_names WHERE name_norm = ? AND is_current = 1)",
            (norm,),
        )
        return Account.from_row(row) if row else None

    def create_account(
        self,
        account_id: str,
        kind: str,
        *,
        xuid: Optional[str] = None,
        player_uuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Account:
        ts = now_ms()
        self.db.insert(
            "accounts",
            {
                "account_id": account_id,
                "kind": kind,
                "xuid": str(xuid) if xuid else None,
                "player_uuid": str(player_uuid) if player_uuid else None,
                "current_name": name,
                "balance_minor": 0,
                "held_minor": 0,
                "status": AccountStatus.ACTIVE.value,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        if name:
            self.link_name(account_id, name, is_current=True)
        return self.get_account(account_id)

    def link_name(self, account_id: str, name: str, *, is_current: bool) -> None:
        """登记玩家名。旧名字保留在表里，只是不再是 current —— 便于改名溯源。"""
        norm = normalize_name(name)
        if not norm:
            return
        ts = now_ms()
        row = self.db.query_one(
            "SELECT account_id, is_current FROM account_names WHERE account_id = ? AND name_norm = ?",
            (account_id, norm),
        )
        if row is None:
            self.db.insert(
                "account_names",
                {
                    "account_id": account_id,
                    "name_norm": norm,
                    "display_name": name,
                    "is_current": 1 if is_current else 0,
                    "first_seen_ms": ts,
                    "last_seen_ms": ts,
                },
            )
        else:
            self.db.update(
                "account_names",
                {"display_name": name, "last_seen_ms": ts, **({"is_current": 1} if is_current else {})},
                "account_id = ? AND name_norm = ?",
                (account_id, norm),
            )

    def set_current_name(self, account_id: str, name: str) -> None:
        """玩家改名：旧名降级为历史名，新名置为当前名。账户与余额完全不动。"""
        norm = normalize_name(name)
        if not norm:
            return
        self.db.update(
            "account_names", {"is_current": 0}, "account_id = ? AND is_current = 1", (account_id,)
        )
        self.link_name(account_id, name, is_current=True)
        self.db.update(
            "accounts",
            {"current_name": name, "updated_at_ms": now_ms()},
            "account_id = ?",
            (account_id,),
        )

    def touch_account(self, account_id: str) -> None:
        self.db.update("accounts", {"updated_at_ms": now_ms()}, "account_id = ?", (account_id,))

    def set_status(self, account_id: str, status: str) -> int:
        return self.db.update(
            "accounts",
            {"status": status, "updated_at_ms": now_ms()},
            "account_id = ?",
            (account_id,),
        )

    def apply_balance_delta(
        self,
        account_id: str,
        *,
        balance_delta: int = 0,
        held_delta: int = 0,
    ) -> int:
        """原子调整余额并返回调整后的 balance。

        负余额保护交给上层（先校验再扣），这里只做纯粹的加减 + 版本自增，
        保证同一事务内多次调整的最终值正确。
        """
        self.db.execute(
            """UPDATE accounts
                  SET balance_minor = balance_minor + ?,
                      held_minor    = held_minor + ?,
                      version       = version + 1,
                      updated_at_ms = ?
                WHERE account_id = ?""",
            (int(balance_delta), int(held_delta), now_ms(), account_id),
        )
        value = self.db.scalar(
            "SELECT balance_minor FROM accounts WHERE account_id = ?", (account_id,), default=0
        )
        return int(value or 0)

    def read_balance(self, account_id: str) -> tuple:
        row = self.db.query_one(
            "SELECT balance_minor, held_minor FROM accounts WHERE account_id = ?", (account_id,)
        )
        if row is None:
            return (0, 0)
        return (int(row["balance_minor"]), int(row["held_minor"]))

    def list_player_accounts(self, *, active_only: bool = False) -> List[Account]:
        sql = "SELECT * FROM accounts WHERE kind = ?"
        params: list = [AccountKind.PLAYER.value]
        if active_only:
            sql += " AND status = ?"
            params.append(AccountStatus.ACTIVE.value)
        return [Account.from_row(r) for r in self.db.query_all(sql, tuple(params))]

    def total_player_balance(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts WHERE kind = ?",
            (AccountKind.PLAYER.value,),
            default=0,
        )
        return int(value or 0)

    def total_agent_balance(self) -> int:
        """非系统主体持有的货币总量：玩家 + 公司。

        这是 M0 的正确口径。**必须包含公司** —— 公司账户里的钱同样是真实流通的货币，
        漏掉它会让「M0 + 国库 + 托管 + 公司 = 累计发行」这条恒等式不成立，
        也会让通胀指标偏低。
        """
        value = self.db.scalar(
            "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts WHERE kind IN (?, ?)",
            (AccountKind.PLAYER.value, AccountKind.COMPANY.value),
            default=0,
        )
        return int(value or 0)

    # ==================== 系统账户 ====================

    def ensure_system_accounts(self) -> None:
        """保证铸币账户与国库存在。缺了账本就无法记账。"""
        for account_id, name in (
            (SYSTEM_MINT_ACCOUNT_ID, "货币发行"),
            (TREASURY_ACCOUNT_ID, "国库"),
        ):
            if self.get_account(account_id) is None:
                self.create_account(account_id, AccountKind.SYSTEM.value, name=name)

    def minted_total(self) -> int:
        """累计净发行量 = 铸币账户的透支额。"""
        row = self.db.query_one(
            "SELECT balance_minor FROM accounts WHERE account_id = ?", (SYSTEM_MINT_ACCOUNT_ID,)
        )
        if row is None:
            return 0
        return max(0, -int(row["balance_minor"]))

    # ==================== 账本 ====================

    def create_tx(
        self,
        tx_id: str,
        idempotency_key: str,
        kind: str,
        *,
        status: str = TxStatus.FILLED.value,
        supply_delta_minor: int = 0,
        correlation_id: Optional[str] = None,
        actor_account_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        import json

        ts = now_ms()
        committed = ts if status == TxStatus.FILLED.value else None
        self.db.insert(
            "ledger_tx",
            {
                "tx_id": tx_id,
                "idempotency_key": idempotency_key,
                "kind": kind,
                "status": status,
                "supply_delta_minor": int(supply_delta_minor),
                "correlation_id": correlation_id,
                "actor_account_id": actor_account_id,
                "metadata_json": json.dumps(metadata, ensure_ascii=False) if metadata else None,
                "created_at_ms": ts,
                "committed_at_ms": committed,
            },
        )

    def set_tx_status(self, tx_id: str, status: str) -> int:
        committed = now_ms() if status == TxStatus.FILLED.value else None
        return self.db.update(
            "ledger_tx",
            {"status": status, "committed_at_ms": committed},
            "tx_id = ?",
            (tx_id,),
        )

    def find_tx_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM ledger_tx WHERE idempotency_key = ?", (idempotency_key,)
        )

    def add_entry(
        self, tx_id: str, account_id: str, role: str, delta_minor: int, balance_after_minor: int
    ) -> None:
        self.db.insert(
            "ledger_entries",
            {
                "tx_id": tx_id,
                "account_id": account_id,
                "role": role,
                "delta_minor": int(delta_minor),
                "balance_after_minor": int(balance_after_minor),
                "created_at_ms": now_ms(),
            },
        )

    def entry_sum(self, tx_id: str) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(delta_minor), 0) FROM ledger_entries WHERE tx_id = ?",
            (tx_id,),
            default=0,
        )
        return int(value or 0)

    # ==================== 查询 ====================

    def history(self, account_id: str, *, limit: int = 20, offset: int = 0) -> List[TxRecord]:
        rows = self.db.query_all(
            """SELECT e.tx_id          AS tx_id,
                      t.kind           AS kind,
                      t.status         AS status,
                      e.role           AS role,
                      e.delta_minor    AS delta_minor,
                      e.balance_after_minor AS balance_after_minor,
                      t.correlation_id AS correlation_id,
                      t.metadata_json  AS metadata_json,
                      t.created_at_ms  AS created_at_ms
                 FROM ledger_entries e
                 JOIN ledger_tx t ON t.tx_id = e.tx_id
                WHERE e.account_id = ?
                ORDER BY e.entry_id DESC
                LIMIT ? OFFSET ?""",
            (account_id, int(limit), int(offset)),
        )
        return [
            TxRecord(
                tx_id=r["tx_id"],
                kind=r["kind"],
                status=r["status"],
                role=r["role"],
                delta_minor=int(r["delta_minor"]),
                balance_after_minor=int(r["balance_after_minor"]),
                correlation_id=r["correlation_id"],
                metadata_json=r["metadata_json"],
                created_at_ms=int(r["created_at_ms"]),
            )
            for r in rows
        ]

    def count_entries(self, account_id: str) -> int:
        value = self.db.scalar(
            "SELECT COUNT(*) FROM ledger_entries WHERE account_id = ?", (account_id,), default=0
        )
        return int(value or 0)

    # ==================== 一致性自检 ====================

    def audit_unbalanced(self, *, limit: int = 50) -> List[dict]:
        """找出分录不平的交易。正常应当恒为空。"""
        return self.db.query_all(
            """SELECT t.tx_id, t.kind, t.status, SUM(e.delta_minor) AS total
                 FROM ledger_tx t
                 JOIN ledger_entries e ON e.tx_id = t.tx_id
                GROUP BY t.tx_id
               HAVING total != 0
                LIMIT ?""",
            (int(limit),),
        )

    def audit_balance_drift(self, *, limit: int = 50) -> List[dict]:
        """找出「账户表余额」与「账本最后一条分录的结余」不一致的账户。"""
        return self.db.query_all(
            """SELECT a.account_id, a.balance_minor AS table_balance,
                      COALESCE((SELECT e.balance_after_minor
                                  FROM ledger_entries e
                                 WHERE e.account_id = a.account_id
                                 ORDER BY e.entry_id DESC LIMIT 1), 0) AS ledger_balance
                 FROM accounts a
                WHERE a.balance_minor != COALESCE(
                        (SELECT e.balance_after_minor
                           FROM ledger_entries e
                          WHERE e.account_id = a.account_id
                          ORDER BY e.entry_id DESC LIMIT 1), 0)
                LIMIT ?""",
            (int(limit),),
        )
