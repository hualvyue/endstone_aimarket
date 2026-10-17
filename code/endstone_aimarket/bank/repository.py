"""央行的持久化层。

与其它模块保持一致：**所有写方法都假定调用方已处在事务里**，这里不自己 commit，
原子性由上层统一控制。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import (
    AccountKind,
    CreditStatus,
    DepositStatus,
    MortgageStatus,
)
from ..core.database import Database
from ..core.ids import new_uuid7
from .models import (
    Alert,
    Card,
    CreditLine,
    Deposit,
    Governor,
    Mortgage,
    MortgageSchedule,
    OmoRecord,
    PolicyState,
    Reserve,
    Statement,
)


def now_ms() -> int:
    return int(time.time() * 1000)


HOUR_MS = 3_600_000
DAY_MS = 86_400_000


class BankRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 行长 ====================

    def get_governor(self, account_id: str) -> Optional[Governor]:
        row = self.db.query_one(
            "SELECT * FROM bank_governors WHERE account_id = ?", (account_id,)
        )
        return Governor.from_row(row) if row else None

    def get_governor_by_xuid(self, xuid: str) -> Optional[Governor]:
        if not xuid:
            return None
        row = self.db.query_one(
            "SELECT * FROM bank_governors WHERE xuid = ? AND revoked_at_ms IS NULL",
            (str(xuid),),
        )
        return Governor.from_row(row) if row else None

    def list_governors(self, *, only_active: bool = True) -> List[Governor]:
        if only_active:
            rows = self.db.query_all(
                "SELECT * FROM bank_governors WHERE revoked_at_ms IS NULL "
                "ORDER BY granted_at_ms"
            )
        else:
            rows = self.db.query_all("SELECT * FROM bank_governors ORDER BY granted_at_ms")
        return [Governor.from_row(r) for r in rows]

    def upsert_governor(self, account_id: str, xuid: str, display_name: Optional[str] = None,
                        note: Optional[str] = None) -> Governor:
        """幂等登记：已存在则复活并更新名字，不存在则新建。"""
        ts = now_ms()
        existing = self.db.query_one(
            "SELECT * FROM bank_governors WHERE account_id = ?", (account_id,)
        )
        if existing is None:
            self.db.insert(
                "bank_governors",
                {
                    "account_id": account_id,
                    "xuid": str(xuid),
                    "display_name": display_name,
                    "granted_at_ms": ts,
                    "revoked_at_ms": None,
                    "note": note,
                },
            )
        else:
            self.db.update(
                "bank_governors",
                {
                    "xuid": str(xuid),
                    "display_name": display_name if display_name else existing["display_name"],
                    "revoked_at_ms": None,
                    "note": note if note is not None else existing["note"],
                },
                "account_id = ?",
                (account_id,),
            )
        return self.get_governor(account_id)

    def revoke_governor(self, account_id: str) -> bool:
        changed = self.db.update(
            "bank_governors",
            {"revoked_at_ms": now_ms()},
            "account_id = ? AND revoked_at_ms IS NULL",
            (account_id,),
        )
        return bool(changed)

    def is_governor_xuid(self, xuid: str) -> bool:
        return self.get_governor_by_xuid(xuid) is not None

    # ==================== 政策状态 ====================

    def get_policy(self) -> PolicyState:
        """取政策状态。首次访问时落一行默认值。"""
        row = self.db.query_one("SELECT * FROM bank_policy_state WHERE id = 1")
        if row is None:
            ts = now_ms()
            self.db.insert(
                "bank_policy_state",
                {"id": 1, "created_at_ms": ts, "updated_at_ms": ts},
            )
            row = self.db.query_one("SELECT * FROM bank_policy_state WHERE id = 1")
        return PolicyState.from_row(row)

    def save_policy(self, policy: PolicyState) -> None:
        policy.updated_at_ms = now_ms()
        self.db.update(
            "bank_policy_state",
            {
                "base_rate_bps_per_day": int(policy.base_rate_bps_per_day),
                "rrr_bps": int(policy.rrr_bps),
                "discount_wedge_bps": int(policy.discount_wedge_bps),
                "demand_spread_bps": int(policy.demand_spread_bps),
                "term_premium_bps": int(policy.term_premium_bps),
                "credit_spread_bps": int(policy.credit_spread_bps),
                "od_premium_bps": int(policy.od_premium_bps),
                "card_premium_bps": int(policy.card_premium_bps),
                "target_inflation_bps": int(policy.target_inflation_bps),
                "target_band_bps": int(policy.target_band_bps),
                "auto_policy": 1 if policy.auto_policy else 0,
                "max_step_bps": int(policy.max_step_bps),
                "adjust_cooldown_ms": int(policy.adjust_cooldown_ms),
                "omo_max_per_op_minor": int(policy.omo_max_per_op_minor),
                "qe_cap_bps": int(policy.qe_cap_bps),
                "qe_used_minor": int(policy.qe_used_minor),
                "reserve_floor_car_bps": int(policy.reserve_floor_car_bps),
                "locked_json": json.dumps(policy.locked, ensure_ascii=False) if policy.locked else None,
                "last_adjust_ms": int(policy.last_adjust_ms),
                "updated_at_ms": policy.updated_at_ms,
            },
            "id = 1",
            (),
        )

    # ==================== 存款 ====================

    def create_deposit(
        self,
        *,
        holder_account_id: str,
        deposit_account_id: str,
        kind: str,
        principal_minor: int,
        rate_bps_per_day: int = 0,
        locked_until_ms: int = 0,
        product_id: Optional[str] = None,
    ) -> Deposit:
        ts = now_ms()
        deposit_id = new_uuid7()
        self.db.insert(
            "bank_deposits",
            {
                "deposit_id": deposit_id,
                "holder_account_id": holder_account_id,
                "deposit_account_id": deposit_account_id,
                "kind": kind,
                "principal_minor": int(principal_minor),
                "rate_bps_per_day": int(rate_bps_per_day),
                "locked_until_ms": int(locked_until_ms),
                "accrued_minor": 0,
                "paid_interest_minor": 0,
                "last_accrual_ms": ts,
                "product_id": product_id,
                "status": DepositStatus.ACTIVE.value,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_deposit(deposit_id)

    def get_deposit(self, deposit_id: str) -> Optional[Deposit]:
        row = self.db.query_one(
            "SELECT * FROM bank_deposits WHERE deposit_id = ?", (deposit_id,)
        )
        return Deposit.from_row(row) if row else None

    def deposits_of(
        self,
        holder_account_id: str,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Deposit]:
        sql = "SELECT * FROM bank_deposits WHERE holder_account_id = ?"
        params: List[Any] = [holder_account_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at_ms DESC"
        return [Deposit.from_row(r) for r in self.db.query_all(sql, tuple(params))]

    def active_deposits_of(self, holder_account_id: str) -> List[Deposit]:
        return self.deposits_of(holder_account_id, status=DepositStatus.ACTIVE.value)

    def list_active_deposits(self, *, kind: Optional[str] = None) -> List[Deposit]:
        """全部活跃存款：计息任务与挤兑统计用。"""
        if kind:
            rows = self.db.query_all(
                "SELECT * FROM bank_deposits WHERE status = ? AND kind = ?",
                (DepositStatus.ACTIVE.value, kind),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_deposits WHERE status = ?",
                (DepositStatus.ACTIVE.value,),
            )
        return [Deposit.from_row(r) for r in rows]

    def matured_fixed_deposits(self, now: Optional[int] = None) -> List[Deposit]:
        """到期的定期/理财，供定时任务自动转活期。"""
        ts = now if now is not None else now_ms()
        rows = self.db.query_all(
            "SELECT * FROM bank_deposits "
            "WHERE status = ? AND kind IN ('time','wealth') AND locked_until_ms > 0 "
            "AND locked_until_ms <= ?",
            (DepositStatus.ACTIVE.value, ts),
        )
        return [Deposit.from_row(r) for r in rows]

    def update_deposit(self, deposit_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update("bank_deposits", fields, "deposit_id = ?", (deposit_id,))
        return bool(changed)

    # ---------- 计息幂等 ----------

    def has_accrual(self, deposit_id: str, bucket_ms: int) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM bank_interest_accruals WHERE deposit_id = ? AND bucket_ms = ?",
            (deposit_id, int(bucket_ms)),
        )
        return row is not None

    def record_accrual(self, deposit_id: str, bucket_ms: int, interest_minor: int, hours: int) -> None:
        self.db.insert(
            "bank_interest_accruals",
            {
                "deposit_id": deposit_id,
                "bucket_ms": int(bucket_ms),
                "interest_minor": int(interest_minor),
                "hours": int(hours),
                "created_at_ms": now_ms(),
            },
        )

    def deposit_totals(self) -> Dict[str, int]:
        """按产品类型汇总存款余额。

        accounts.kind 是 'deposit'（账户类型），产品类型在 bank_deposits.kind 上，
        所以按 bank_deposits 关联统计才是对的。
        """
        rows = self.db.query_all(
            """SELECT d.kind AS dkind, COALESCE(SUM(a.balance_minor), 0) AS total
                 FROM bank_deposits d
                 JOIN accounts a ON a.account_id = d.deposit_account_id
                WHERE d.status = ?
                GROUP BY d.kind""",
            (DepositStatus.ACTIVE.value,),
        )
        result = {"demand": 0, "time": 0, "wealth": 0}
        for row in rows:
            key = str(row["dkind"])
            result[key] = result.get(key, 0) + int(row["total"] or 0)
        return result

    def accrued_interest_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(accrued_minor), 0) FROM bank_deposits WHERE status = ?",
            (DepositStatus.ACTIVE.value,),
            default=0,
        )
        return int(value or 0)

    def deposit_account_count(self) -> int:
        value = self.db.scalar(
            "SELECT COUNT(*) FROM bank_deposits WHERE status = ?",
            (DepositStatus.ACTIVE.value,),
            default=0,
        )
        return int(value or 0)

    # ==================== 透支授信 ====================

    def get_credit_line(self, holder_account_id: str) -> Optional[CreditLine]:
        row = self.db.query_one(
            "SELECT * FROM bank_credit_lines WHERE holder_account_id = ?",
            (holder_account_id,),
        )
        return CreditLine.from_row(row) if row else None

    def create_credit_line(
        self,
        *,
        holder_account_id: str,
        credit_limit_minor: int,
        rate_bps_per_day: int,
        due_at_ms: int = 0,
    ) -> CreditLine:
        ts = now_ms()
        line_id = new_uuid7()
        self.db.insert(
            "bank_credit_lines",
            {
                "line_id": line_id,
                "holder_account_id": holder_account_id,
                "credit_limit_minor": int(credit_limit_minor),
                "used_minor": 0,
                "rate_bps_per_day": int(rate_bps_per_day),
                "due_at_ms": int(due_at_ms),
                "autopay": 1,
                "status": CreditStatus.ACTIVE.value,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_credit_line(holder_account_id)

    def update_credit_line(self, line_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update("bank_credit_lines", fields, "line_id = ?", (line_id,))
        return bool(changed)

    def used_credit_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(used_minor), 0) FROM bank_credit_lines WHERE status = ?",
            (CreditStatus.ACTIVE.value,),
            default=0,
        )
        return int(value or 0)

    def overdue_credit_lines(self, now: Optional[int] = None) -> List[CreditLine]:
        ts = now if now is not None else now_ms()
        rows = self.db.query_all(
            "SELECT * FROM bank_credit_lines WHERE status = ? AND used_minor > 0 "
            "AND due_at_ms > 0 AND due_at_ms <= ?",
            (CreditStatus.ACTIVE.value, ts),
        )
        return [CreditLine.from_row(r) for r in rows]

    # ==================== 信用卡 ====================

    def create_card(
        self,
        *,
        holder_account_id: str,
        credit_limit_minor: int,
        apr_bps_per_day: int,
        penalty_bps_per_day: int,
        cycle_days: int,
        min_pay_bps: int,
        grace_days: int = 0,
        next_statement_ms: int = 0,
    ) -> Card:
        ts = now_ms()
        card_id = new_uuid7()
        self.db.insert(
            "bank_cards",
            {
                "card_id": card_id,
                "holder_account_id": holder_account_id,
                "credit_limit_minor": int(credit_limit_minor),
                "used_minor": 0,
                "apr_bps_per_day": int(apr_bps_per_day),
                "penalty_bps_per_day": int(penalty_bps_per_day),
                "cycle_days": int(cycle_days),
                "min_pay_bps": int(min_pay_bps),
                "grace_days": int(grace_days),
                "next_statement_ms": int(next_statement_ms),
                "status": CreditStatus.ACTIVE.value,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_card(card_id)

    def get_card(self, card_id: str) -> Optional[Card]:
        row = self.db.query_one("SELECT * FROM bank_cards WHERE card_id = ?", (card_id,))
        return Card.from_row(row) if row else None

    def cards_of(self, holder_account_id: str, *, status: Optional[str] = None) -> List[Card]:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM bank_cards WHERE holder_account_id = ? AND status = ? "
                "ORDER BY created_at_ms",
                (holder_account_id, status),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_cards WHERE holder_account_id = ? ORDER BY created_at_ms",
                (holder_account_id,),
            )
        return [Card.from_row(r) for r in rows]

    def update_card(self, card_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update("bank_cards", fields, "card_id = ?", (card_id,))
        return bool(changed)

    def cards_due_statement(self, now: Optional[int] = None) -> List[Card]:
        """该出账单的卡。"""
        ts = now if now is not None else now_ms()
        rows = self.db.query_all(
            "SELECT * FROM bank_cards WHERE status = ? AND next_statement_ms > 0 "
            "AND next_statement_ms <= ?",
            (CreditStatus.ACTIVE.value, ts),
        )
        return [Card.from_row(r) for r in rows]

    def used_card_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(used_minor), 0) FROM bank_cards WHERE status = ?",
            (CreditStatus.ACTIVE.value,),
            default=0,
        )
        return int(value or 0)

    # ---------- 账单 ----------

    def create_statement(
        self,
        *,
        card_id: str,
        period_end_ms: int,
        due_at_ms: int,
        amount_minor: int,
        min_pay_minor: int,
    ) -> Statement:
        ts = now_ms()
        statement_id = new_uuid7()
        self.db.insert(
            "bank_card_statements",
            {
                "statement_id": statement_id,
                "card_id": card_id,
                "period_end_ms": int(period_end_ms),
                "due_at_ms": int(due_at_ms),
                "amount_minor": int(amount_minor),
                "min_pay_minor": int(min_pay_minor),
                "paid_minor": 0,
                "penalty_minor": 0,
                "status": "open",
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_statement(statement_id)

    def get_statement(self, statement_id: str) -> Optional[Statement]:
        row = self.db.query_one(
            "SELECT * FROM bank_card_statements WHERE statement_id = ?", (statement_id,)
        )
        return Statement.from_row(row) if row else None

    def statements_of(self, card_id: str, *, status: Optional[str] = None) -> List[Statement]:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM bank_card_statements WHERE card_id = ? AND status = ? "
                "ORDER BY period_end_ms DESC",
                (card_id, status),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_card_statements WHERE card_id = ? ORDER BY period_end_ms DESC",
                (card_id,),
            )
        return [Statement.from_row(r) for r in rows]

    def open_statements(self, card_id: str) -> List[Statement]:
        rows = self.db.query_all(
            "SELECT * FROM bank_card_statements WHERE card_id = ? AND status IN ('open','overdue') "
            "ORDER BY due_at_ms",
            (card_id,),
        )
        return [Statement.from_row(r) for r in rows]

    def due_statements(self, now: Optional[int] = None) -> List[Statement]:
        ts = now if now is not None else now_ms()
        rows = self.db.query_all(
            "SELECT * FROM bank_card_statements WHERE status = 'open' AND due_at_ms > 0 "
            "AND due_at_ms <= ?",
            (ts,),
        )
        return [Statement.from_row(r) for r in rows]

    def update_statement(self, statement_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update(
            "bank_card_statements", fields, "statement_id = ?", (statement_id,)
        )
        return bool(changed)

    # ==================== 按揭 ====================

    def create_mortgage(
        self,
        *,
        borrower_account_id: str,
        collateral_kind: str,
        collateral_ref: str,
        collateral_value_minor: int,
        principal_minor: int,
        rate_bps_per_day: int,
        term_days: int,
        periods: int,
        next_due_ms: int,
        loan_id: Optional[str] = None,
    ) -> Mortgage:
        ts = now_ms()
        mortgage_id = new_uuid7()
        self.db.insert(
            "bank_mortgages",
            {
                "mortgage_id": mortgage_id,
                "borrower_account_id": borrower_account_id,
                "loan_id": loan_id,
                "collateral_kind": collateral_kind,
                "collateral_ref": collateral_ref,
                "collateral_value_minor": int(collateral_value_minor),
                "principal_minor": int(principal_minor),
                "outstanding_minor": int(principal_minor),
                "rate_bps_per_day": int(rate_bps_per_day),
                "term_days": int(term_days),
                "periods": int(periods),
                "next_due_ms": int(next_due_ms),
                "status": MortgageStatus.ACTIVE.value,
                "version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get_mortgage(mortgage_id)

    def get_mortgage(self, mortgage_id: str) -> Optional[Mortgage]:
        row = self.db.query_one(
            "SELECT * FROM bank_mortgages WHERE mortgage_id = ?", (mortgage_id,)
        )
        return Mortgage.from_row(row) if row else None

    def mortgages_of(self, borrower_account_id: str, *, active_only: bool = False) -> List[Mortgage]:
        if active_only:
            rows = self.db.query_all(
                "SELECT * FROM bank_mortgages WHERE borrower_account_id = ? "
                "AND status IN ('active','overdue') ORDER BY created_at_ms DESC",
                (borrower_account_id,),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_mortgages WHERE borrower_account_id = ? "
                "ORDER BY created_at_ms DESC",
                (borrower_account_id,),
            )
        return [Mortgage.from_row(r) for r in rows]

    def collateral_in_use(self, collateral_ref: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM bank_mortgages WHERE collateral_ref = ? "
            "AND status IN ('active','overdue')",
            (collateral_ref,),
        )
        return row is not None

    def update_mortgage(self, mortgage_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update("bank_mortgages", fields, "mortgage_id = ?", (mortgage_id,))
        return bool(changed)

    def outstanding_mortgage_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(outstanding_minor), 0) FROM bank_mortgages "
            "WHERE status IN ('active','overdue')",
            default=0,
        )
        return int(value or 0)

    def mortgages_of_all_active(self) -> List[Mortgage]:
        """全部在贷按揭：到期检查任务用。"""
        rows = self.db.query_all(
            "SELECT * FROM bank_mortgages WHERE status IN ('active','overdue') "
            "ORDER BY created_at_ms"
        )
        return [Mortgage.from_row(r) for r in rows]

    def overdue_mortgages(self, now: Optional[int] = None) -> List[Mortgage]:
        ts = now if now is not None else now_ms()
        rows = self.db.query_all(
            "SELECT * FROM bank_mortgages WHERE status IN ('active','overdue') "
            "AND next_due_ms > 0 AND next_due_ms <= ?",
            (ts,),
        )
        return [Mortgage.from_row(r) for r in rows]

    # ---------- 还款计划 ----------

    def create_schedules(self, mortgage_id: str, schedules: List[Dict[str, Any]]) -> None:
        ts = now_ms()
        for item in schedules:
            self.db.insert(
                "bank_mortgage_schedules",
                {
                    "mortgage_id": mortgage_id,
                    "period_index": int(item["period_index"]),
                    "due_at_ms": int(item.get("due_at_ms", 0)),
                    "principal_minor": int(item.get("principal_minor", 0)),
                    "interest_minor": int(item.get("interest_minor", 0)),
                    "paid_minor": 0,
                    "status": "pending",
                },
            )

    def schedules_of(self, mortgage_id: str) -> List[MortgageSchedule]:
        rows = self.db.query_all(
            "SELECT * FROM bank_mortgage_schedules WHERE mortgage_id = ? ORDER BY period_index",
            (mortgage_id,),
        )
        return [MortgageSchedule.from_row(r) for r in rows]

    def next_pending_schedule(self, mortgage_id: str) -> Optional[MortgageSchedule]:
        row = self.db.query_one(
            "SELECT * FROM bank_mortgage_schedules WHERE mortgage_id = ? AND status = 'pending' "
            "ORDER BY period_index LIMIT 1",
            (mortgage_id,),
        )
        return MortgageSchedule.from_row(row) if row else None

    def update_schedule(self, mortgage_id: str, period_index: int, fields: Dict[str, Any]) -> bool:
        changed = self.db.update(
            "bank_mortgage_schedules",
            dict(fields),
            "mortgage_id = ? AND period_index = ?",
            (mortgage_id, int(period_index)),
        )
        return bool(changed)

    # ==================== 理财 ====================

    def upsert_product(
        self,
        *,
        product_id: str,
        name: str,
        strategy_key: str = "fixed",
        risk_level: int = 1,
        min_amount_minor: int = 0,
        term_days: int = 7,
        expected_bps: int = 0,
    ) -> None:
        existing = self.db.query_one(
            "SELECT product_id FROM bank_wealth_products WHERE product_id = ?", (product_id,)
        )
        if existing is None:
            self.db.insert(
                "bank_wealth_products",
                {
                    "product_id": product_id,
                    "name": name,
                    "strategy_key": strategy_key,
                    "risk_level": int(risk_level),
                    "min_amount_minor": int(min_amount_minor),
                    "term_days": int(term_days),
                    "expected_bps": int(expected_bps),
                    "status": "active",
                    "created_at_ms": now_ms(),
                },
            )
        else:
            self.db.update(
                "bank_wealth_products",
                {
                    "name": name,
                    "strategy_key": strategy_key,
                    "risk_level": int(risk_level),
                    "min_amount_minor": int(min_amount_minor),
                    "term_days": int(term_days),
                    "expected_bps": int(expected_bps),
                },
                "product_id = ?",
                (product_id,),
            )

    def list_products(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        if active_only:
            rows = self.db.query_all(
                "SELECT * FROM bank_wealth_products WHERE status = 'active' ORDER BY risk_level, product_id"
            )
        else:
            rows = self.db.query_all("SELECT * FROM bank_wealth_products ORDER BY product_id")
        return [dict(r) for r in rows]

    def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM bank_wealth_products WHERE product_id = ?", (product_id,)
        )
        return dict(row) if row else None

    def create_holding(
        self,
        *,
        product_id: str,
        holder_account_id: str,
        amount_minor: int,
        deposit_id: Optional[str] = None,
        return_bps: int = 0,
        matures_at_ms: int = 0,
    ) -> str:
        ts = now_ms()
        holding_id = new_uuid7()
        self.db.insert(
            "bank_wealth_holdings",
            {
                "holding_id": holding_id,
                "product_id": product_id,
                "holder_account_id": holder_account_id,
                "deposit_id": deposit_id,
                "amount_minor": int(amount_minor),
                "settled_minor": 0,
                "return_bps": int(return_bps),
                "matures_at_ms": int(matures_at_ms),
                "status": "active",
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return holding_id

    def holdings_of(self, holder_account_id: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM bank_wealth_holdings WHERE holder_account_id = ? AND status = ? "
                "ORDER BY created_at_ms DESC",
                (holder_account_id, status),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_wealth_holdings WHERE holder_account_id = ? "
                "ORDER BY created_at_ms DESC",
                (holder_account_id,),
            )
        return [dict(r) for r in rows]

    def update_holding(self, holding_id: str, fields: Dict[str, Any]) -> bool:
        fields = dict(fields)
        fields["updated_at_ms"] = now_ms()
        changed = self.db.update(
            "bank_wealth_holdings", fields, "holding_id = ?", (holding_id,)
        )
        return bool(changed)

    # ==================== 实物储备与公开市场操作 ====================

    def get_reserve(self, item_key: str) -> Optional[Reserve]:
        row = self.db.query_one(
            "SELECT * FROM bank_reserves WHERE item_key = ?", (item_key,)
        )
        return Reserve.from_row(row) if row else None

    def list_reserves(self, *, only_positive: bool = True) -> List[Reserve]:
        if only_positive:
            rows = self.db.query_all("SELECT * FROM bank_reserves WHERE qty > 0 ORDER BY item_key")
        else:
            rows = self.db.query_all("SELECT * FROM bank_reserves ORDER BY item_key")
        return [Reserve.from_row(r) for r in rows]

    def adjust_reserve(
        self,
        item_key: str,
        *,
        qty_delta: int,
        value_delta: int,
        item_name: Optional[str] = None,
    ) -> None:
        """增减储备。数量与账面成本一起动，卖出时按账面单位成本结转。"""
        ts = now_ms()
        existing = self.get_reserve(item_key)
        if existing is None:
            self.db.insert(
                "bank_reserves",
                {
                    "item_key": item_key,
                    "item_name": item_name,
                    "qty": max(0, int(qty_delta)),
                    "book_value_minor": max(0, int(value_delta)),
                    "last_op_ms": ts,
                    "updated_at_ms": ts,
                },
            )
            return
        new_qty = max(0, existing.qty + int(qty_delta))
        new_value = existing.book_value_minor + int(value_delta)
        if new_qty <= 0:
            new_qty = 0
            new_value = 0
        elif new_value < 0:
            new_value = 0
        self.db.update(
            "bank_reserves",
            {
                "qty": new_qty,
                "book_value_minor": new_value,
                "item_name": item_name or existing.item_name,
                "last_op_ms": ts,
                "updated_at_ms": ts,
            },
            "item_key = ?",
            (item_key,),
        )

    def reserve_value_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(book_value_minor), 0) FROM bank_reserves WHERE qty > 0", default=0
        )
        return int(value or 0)

    def record_omo(
        self,
        *,
        side: str,
        item_key: str,
        qty: int,
        unit_minor: int,
        amount_minor: int,
        price_bps: int = 0,
        is_qe: bool = False,
        actor_account_id: Optional[str] = None,
        ledger_tx_id: Optional[str] = None,
        m0_before_minor: int = 0,
        m0_after_minor: int = 0,
        detail: Optional[str] = None,
    ) -> str:
        op_id = new_uuid7()
        self.db.insert(
            "bank_omo_log",
            {
                "op_id": op_id,
                "side": side,
                "item_key": item_key,
                "qty": int(qty),
                "unit_minor": int(unit_minor),
                "amount_minor": int(amount_minor),
                "price_bps": int(price_bps),
                "is_qe": 1 if is_qe else 0,
                "actor_account_id": actor_account_id,
                "ledger_tx_id": ledger_tx_id,
                "m0_before_minor": int(m0_before_minor),
                "m0_after_minor": int(m0_after_minor),
                "detail": detail,
                "created_at_ms": now_ms(),
            },
        )
        return op_id

    def recent_omo(self, limit: int = 20) -> List[OmoRecord]:
        rows = self.db.query_all(
            "SELECT * FROM bank_omo_log ORDER BY created_at_ms DESC LIMIT ?", (int(limit),)
        )
        return [OmoRecord.from_row(r) for r in rows]

    def omo_since(self, since_ms: int, *, side: Optional[str] = None) -> List[OmoRecord]:
        if side:
            rows = self.db.query_all(
                "SELECT * FROM bank_omo_log WHERE created_at_ms >= ? AND side = ? "
                "ORDER BY created_at_ms DESC",
                (int(since_ms), side),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_omo_log WHERE created_at_ms >= ? ORDER BY created_at_ms DESC",
                (int(since_ms),),
            )
        return [OmoRecord.from_row(r) for r in rows]

    # ==================== 监管 ====================

    def add_alert(
        self,
        *,
        kind: str,
        title: str,
        severity: str = "warning",
        detail: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        dedupe_ms: int = 0,
    ) -> Optional[str]:
        """记一条告警。`dedupe_ms` 内同类型告警只记一次，避免刷屏。"""
        if dedupe_ms > 0:
            cutoff = now_ms() - int(dedupe_ms)
            existing = self.db.query_one(
                "SELECT alert_id FROM bank_alerts WHERE kind = ? AND created_at_ms >= ? "
                "ORDER BY created_at_ms DESC LIMIT 1",
                (kind, cutoff),
            )
            if existing is not None:
                return None
        alert_id = new_uuid7()
        self.db.insert(
            "bank_alerts",
            {
                "alert_id": alert_id,
                "kind": kind,
                "severity": severity,
                "title": title,
                "detail": detail,
                "metrics_json": json.dumps(metrics, ensure_ascii=False) if metrics else None,
                "acknowledged": 0,
                "created_at_ms": now_ms(),
            },
        )
        return alert_id

    def recent_alerts(self, limit: int = 20) -> List[Alert]:
        rows = self.db.query_all(
            "SELECT * FROM bank_alerts ORDER BY created_at_ms DESC LIMIT ?", (int(limit),)
        )
        return [Alert.from_row(r) for r in rows]

    def unacknowledged_count(self) -> int:
        value = self.db.scalar(
            "SELECT COUNT(*) FROM bank_alerts WHERE acknowledged = 0", default=0
        )
        return int(value or 0)

    def acknowledge_alerts(self, *, kind: Optional[str] = None) -> int:
        if kind:
            return int(self.db.update("bank_alerts", {"acknowledged": 1}, "kind = ? AND acknowledged = 0", (kind,)) or 0)
        return int(self.db.update("bank_alerts", {"acknowledged": 1}, "acknowledged = 0", ()) or 0)

    # ---------- 坏账 ----------

    def add_writeoff(
        self,
        *,
        amount_minor: int,
        loan_id: Optional[str] = None,
        borrower_account_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> str:
        writeoff_id = new_uuid7()
        self.db.insert(
            "bank_writeoffs",
            {
                "writeoff_id": writeoff_id,
                "loan_id": loan_id,
                "borrower_account_id": borrower_account_id,
                "amount_minor": int(amount_minor),
                "reason": reason,
                "created_at_ms": now_ms(),
            },
        )
        return writeoff_id

    def writeoff_total(self, *, since_ms: int = 0) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM bank_writeoffs WHERE created_at_ms >= ?",
            (int(since_ms),),
            default=0,
        )
        return int(value or 0)

    # ---------- 事件 ----------

    def add_event(
        self,
        *,
        kind: str,
        amount_minor: int = 0,
        m0_after_minor: int = 0,
        detail: Optional[str] = None,
    ) -> str:
        event_id = new_uuid7()
        self.db.insert(
            "bank_events",
            {
                "event_id": event_id,
                "kind": kind,
                "amount_minor": int(amount_minor),
                "m0_after_minor": int(m0_after_minor),
                "detail": detail,
                "created_at_ms": now_ms(),
            },
        )
        return event_id

    def recent_events(self, limit: int = 20, *, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind:
            rows = self.db.query_all(
                "SELECT * FROM bank_events WHERE kind = ? ORDER BY created_at_ms DESC LIMIT ?",
                (kind, int(limit)),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM bank_events ORDER BY created_at_ms DESC LIMIT ?", (int(limit),)
            )
        return [dict(r) for r in rows]
