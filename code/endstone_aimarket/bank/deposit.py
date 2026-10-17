"""央行存款业务：活期与定期。

设计要点：
  * **每一笔存款对应一个独立的 `accounts` 行**（kind='deposit'），钱实际躺在那里。
    复式记账原生支持，M1 可以精确统计，而且 `kind != 'system'` 天然禁负。
  * 利率由基准利率派生：活期 = 基准 + 活期利差，定期 = 基准 + 定期加成。
    行长只调基准，全局联动。
  * 计息**按整小时单利**，与贷款系统同一套算法；幂等靠
    `bank_interest_accruals(存款, 时间桶)` 主键，重跑不双记。
  * 利息是央行**增发**出来的（发行科目 → 存款账户），所以它会推高基础货币 ——
    这正是"存款利息增发失控"风险的来源，因此利率有上限钳制。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import DepositStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7
from ..core.money import format_amount
from ..core.result import Result
from ..economy.models import ROLE_PAYEE, ROLE_PAYER, Entry
from ..economy.service import EconomyService
from .models import Deposit
from .repository import DAY_MS, HOUR_MS, now_ms
from .service import (
    CENTRAL_BANK_ISSUE_ACCOUNT_ID,
    EVENT_DEPOSIT,
    EVENT_WITHDRAW,
    CentralBankService,
)


class DepositService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        bank: CentralBankService,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.bank = bank
        self.config = config
        self.log = log
        self.repo = bank.repo

    # ==================== 利率与限额 ====================

    def demand_rate_bps(self) -> int:
        return self.bank.policy().demand_rate_bps()

    def time_rate_bps(self) -> int:
        return self.bank.policy().time_rate_bps()

    def min_deposit_minor(self) -> int:
        if self.config is None:
            return 100
        return max(1, int(self.config.as_int("bank.min_deposit_minor", 100) or 100))

    def min_time_days(self) -> int:
        if self.config is None:
            return 1
        return max(1, int(self.config.as_int("bank.min_time_days", 1) or 1))

    def max_time_days(self) -> int:
        if self.config is None:
            return 365
        return max(1, int(self.config.as_int("bank.max_time_days", 365) or 365))

    def early_penalty_bps(self) -> int:
        if self.config is None:
            return 3000
        return max(0, int(self.config.as_int("bank.early_withdraw_penalty_bps", 3000) or 0))

    def max_deposit_minor(self) -> int:
        """单笔存款上限，0 = 不限。防止一笔巨款把 M1 瞬间拉爆。"""
        if self.config is None:
            return 0
        return max(0, int(self.config.as_int("bank.max_deposit_minor", 0) or 0))

    # ==================== 查询 ====================

    def demand_of(self, account_id: str) -> Optional[Deposit]:
        """取该玩家的活期存款（一个玩家一个活期账户）。"""
        for deposit in self.repo.deposits_of(account_id, kind="demand",
                                             status=DepositStatus.ACTIVE.value):
            return deposit
        return None

    def list_of(self, account_id: str) -> List[Deposit]:
        return self.repo.active_deposits_of(account_id)

    def total_of(self, account_id: str, *, kind: Optional[str] = None) -> int:
        return sum(d.total_minor for d in self.repo.deposits_of(
            account_id, kind=kind, status=DepositStatus.ACTIVE.value))

    def available_of(self, account_id: str, *, now: Optional[int] = None) -> int:
        """可自由支取的金额：活期本金 + 已到期的定期。"""
        current = int(now if now is not None else now_ms())
        total = 0
        for deposit in self.repo.active_deposits_of(account_id):
            if not deposit.is_locked(current):
                total += deposit.total_minor
        return total

    # ==================== 存入 ====================

    def deposit(
        self,
        *,
        holder_account_id: str,
        amount_minor: int,
        kind: str = "demand",
        days: int = 0,
        product_id: Optional[str] = None,
    ) -> Result[Deposit]:
        """存入。kind='demand' 活期、'time' 定期、'wealth' 理财资金池。"""
        amount = int(amount_minor)
        minimum = self.min_deposit_minor()
        if amount < minimum:
            return Result.failure(
                ErrorCode.INVALID_AMOUNT, f"至少要存 {format_amount(minimum)}"
            )
        cap = self.max_deposit_minor()
        if cap > 0 and amount > cap:
            return Result.failure(
                ErrorCode.INVALID_AMOUNT, f"单笔存款上限 {format_amount(cap)}"
            )

        holder = self.economy.repo.get_account(holder_account_id)
        if holder is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if holder.available_minor < amount:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"余额不足，可用 {format_amount(holder.available_minor)}",
            )

        if kind == "demand":
            target = self.demand_of(holder_account_id)
            if target is not None:
                return self._deposit_into(target, holder_account_id, amount)
            rate = self.demand_rate_bps()
            locked_until = 0
        elif kind in ("time", "wealth"):
            if kind == "time":
                days = max(self.min_time_days(), min(self.max_time_days(), int(days or 0) or self.min_time_days()))
            rate = self.time_rate_bps()
            locked_until = now_ms() + int(days) * DAY_MS
        else:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "没有这种存款类型")

        try:
            with self.db.transaction():
                account = self.economy.repo.create_account(
                    account_id=f"dep:{new_uuid7()}", kind="deposit"
                )
                jump = self._move_in(holder_account_id, account.account_id, amount)
                if not jump.ok:
                    raise AimarketError(jump.code, jump.message)
                record = self.repo.create_deposit(
                    holder_account_id=holder_account_id,
                    deposit_account_id=account.account_id,
                    kind=kind,
                    principal_minor=amount,
                    rate_bps_per_day=rate,
                    locked_until_ms=locked_until,
                    product_id=product_id,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        self.bank.record_flow(EVENT_DEPOSIT, amount, detail=f"{kind} 新开户")
        return Result.success(record)

    def _deposit_into(self, deposit: Deposit, holder_account_id: str, amount: int) -> Result[Deposit]:
        """往已有存款里追加（活期专用：定期不追加，各有各的锁定期）。"""
        try:
            with self.db.transaction():
                jump = self._move_in(holder_account_id, deposit.deposit_account_id, amount)
                if not jump.ok:
                    raise AimarketError(jump.code, jump.message)
                self.repo.update_deposit(
                    deposit.deposit_id,
                    {
                        "principal_minor": deposit.principal_minor + amount,
                        "version": deposit.version + 1,
                    },
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        self.bank.record_flow(EVENT_DEPOSIT, amount, detail="活期追加")
        return Result.success(self.repo.get_deposit(deposit.deposit_id))

    def _move_in(self, src: str, dst: str, amount: int) -> Result[None]:
        """把钱从玩家账户挪到存款账户。不动货币总量，只是换个主体持有。"""
        return self.economy.post_in_transaction(
            kind=TxKind.BANK_DEPOSIT_IN.value,
            entries=[
                Entry(src, ROLE_PAYER, -amount),
                Entry(dst, ROLE_PAYEE, amount),
            ],
            idem=f"bank-dep-in:{src}:{dst}:{amount}:{new_uuid7()}",
            tx_id=f"bdi:{new_uuid7()}",
            actor=src,
            correlation_id=dst,
            gross=amount,
            net=amount,
        )

    # ==================== 支取 ====================

    def withdraw(
        self,
        *,
        holder_account_id: str,
        amount_minor: int,
        deposit_id: Optional[str] = None,
        allow_early: bool = False,
        now: Optional[int] = None,
    ) -> Result[Dict[str, Any]]:
        """支取。指定 deposit_id 就是取那笔（定期需 allow_early 才许提前取）。

        提前支取按**活期利率折算 + 违约金**，并把这笔存款结清；
        多发的利息会被销毁（货币回笼）。
        """
        current = int(now if now is not None else now_ms())
        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        if deposit_id:
            deposit = self.repo.get_deposit(deposit_id)
            if deposit is None or deposit.holder_account_id != holder_account_id:
                return Result.failure(ErrorCode.BANK_DEPOSIT_NOT_FOUND)
            if deposit.status != DepositStatus.ACTIVE.value:
                return Result.failure(ErrorCode.BANK_DEPOSIT_NOT_FOUND, "这笔存款已经结清了")
        else:
            deposit = self.demand_of(holder_account_id)
            if deposit is None:
                return Result.failure(ErrorCode.BANK_DEPOSIT_NOT_FOUND, "你还没有活期存款")

        if deposit.is_locked(current):
            if not allow_early:
                return Result.failure(
                    ErrorCode.BANK_DEPOSIT_LOCKED,
                    f"定期还有 {deposit.remaining_days(current)} 天到期。"
                    f"提前支取会按活期利率折算并扣违约金",
                )
            return self._early_withdraw(deposit, holder_account_id, current)

        balance = self.economy.get_balance(deposit.deposit_account_id).balance_minor
        if balance <= 0:
            return Result.failure(ErrorCode.INSUFFICIENT_FUNDS, "这笔存款已经空了")

        if amount > balance:
            amount = balance  # 取超了就给全部，比报错友好

        try:
            with self.db.transaction():
                jump = self._move_out(deposit.deposit_account_id, holder_account_id, amount)
                if not jump.ok:
                    raise AimarketError(jump.code, jump.message)
                # 本金优先扣减，不足的部分视为领走了利息
                principal_part = min(amount, deposit.principal_minor)
                interest_part = amount - principal_part
                fields: Dict[str, Any] = {
                    "principal_minor": deposit.principal_minor - principal_part,
                    "accrued_minor": max(0, deposit.accrued_minor - interest_part),
                    "version": deposit.version + 1,
                }
                remaining = balance - amount
                if remaining <= 0:
                    fields["status"] = DepositStatus.WITHDRAWN.value
                self.repo.update_deposit(deposit.deposit_id, fields)
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        self.bank.record_flow(EVENT_WITHDRAW, amount, detail=f"{deposit.kind} 支取")
        left = self.economy.get_balance(deposit.deposit_account_id).balance_minor
        return Result.success({"taken_minor": amount, "remaining_minor": left})

    def _early_withdraw(self, deposit: Deposit, holder_account_id: str, current: int) -> Result[Dict[str, Any]]:
        """提前支取定期：利息按活期重算，再扣违约金，多的利息销毁回笼。"""
        balance = self.economy.get_balance(deposit.deposit_account_id).balance_minor
        held_hours = max(0, (current - deposit.created_at_ms) // HOUR_MS)
        fair = deposit.principal_minor * self.demand_rate_bps() * held_hours // (10000 * 24)
        penalty = deposit.principal_minor * self.early_penalty_bps() // 10000
        payout = max(0, deposit.principal_minor + fair - penalty)
        payout = min(payout, balance)
        burned = max(0, balance - payout)

        try:
            with self.db.transaction():
                jump = self._move_out(deposit.deposit_account_id, holder_account_id, payout)
                if not jump.ok:
                    raise AimarketError(jump.code, jump.message)
                if burned > 0:
                    # 多发的利息销毁：货币回到发行科目（= 回笼）
                    burn = self.economy.post_in_transaction(
                        kind=TxKind.BANK_DEPOSIT_OUT.value,
                        entries=[
                            Entry(deposit.deposit_account_id, ROLE_PAYER, -burned),
                            Entry(CENTRAL_BANK_ISSUE_ACCOUNT_ID, "sink", burned),
                        ],
                        idem=f"bank-dep-burn:{deposit.deposit_id}:{new_uuid7()}",
                        tx_id=f"bdb:{new_uuid7()}",
                        actor=holder_account_id,
                        correlation_id=deposit.deposit_id,
                        gross=burned,
                        net=burned,
                    )
                    if not burn.ok:
                        raise AimarketError(burn.code, burn.message)
                self.repo.update_deposit(
                    deposit.deposit_id,
                    {
                        "principal_minor": 0,
                        "accrued_minor": 0,
                        "status": DepositStatus.WITHDRAWN.value,
                        "version": deposit.version + 1,
                    },
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        self.bank.record_flow(EVENT_WITHDRAW, payout, detail="定期提前支取")
        return Result.success({
            "taken_minor": payout,
            "penalty_minor": penalty,
            "burned_minor": burned,
            "early": True,
        })

    def _move_out(self, src: str, dst: str, amount: int) -> Result[None]:
        return self.economy.post_in_transaction(
            kind=TxKind.BANK_DEPOSIT_OUT.value,
            entries=[
                Entry(src, ROLE_PAYER, -amount),
                Entry(dst, ROLE_PAYEE, amount),
            ],
            idem=f"bank-dep-out:{src}:{dst}:{amount}:{new_uuid7()}",
            tx_id=f"bdo:{new_uuid7()}",
            actor=dst,
            correlation_id=src,
            gross=amount,
            net=amount,
        )

    # ==================== 计息 ====================

    def accrue_all(self, *, now: Optional[int] = None) -> Dict[str, int]:
        """定时任务：给全部活跃存款计息。"""
        current = int(now if now is not None else now_ms())
        total = 0
        count = 0
        for deposit in self.repo.list_active_deposits():
            interest = self.accrue_one(deposit, now=current)
            if interest > 0:
                total += interest
                count += 1
        return {"interest_minor": total, "count": count}

    def accrue_one(self, deposit: Deposit, *, now: Optional[int] = None) -> int:
        current = int(now if now is not None else now_ms())
        with self.db.transaction():
            fresh = self.repo.get_deposit(deposit.deposit_id)
            if fresh is None or fresh.status != DepositStatus.ACTIVE.value:
                return 0
            if fresh.principal_minor <= 0 or fresh.rate_bps_per_day <= 0:
                return 0
            hours = (current - fresh.last_accrual_ms) // HOUR_MS
            if hours <= 0:
                return 0
            interest = fresh.principal_minor * fresh.rate_bps_per_day * hours // (10000 * 24)
            if interest <= 0:
                self.repo.update_deposit(
                    fresh.deposit_id,
                    {"last_accrual_ms": fresh.last_accrual_ms + hours * HOUR_MS},
                )
                return 0

            bucket = (fresh.last_accrual_ms // HOUR_MS) * HOUR_MS
            if self.repo.has_accrual(fresh.deposit_id, bucket):
                return 0  # 同一时间桶已经计过，重跑不双记

            # 利息由央行增发：发行科目 → 存款账户。
            # 这会让基础货币上升，所以利率上限与单周期增发上限是必须的防线。
            posted = self.economy.post_in_transaction(
                kind=TxKind.BANK_INTEREST.value,
                entries=[
                    Entry(CENTRAL_BANK_ISSUE_ACCOUNT_ID, "source", -interest),
                    Entry(fresh.deposit_account_id, ROLE_PAYEE, interest),
                ],
                idem=f"bank-interest:{fresh.deposit_id}:{bucket}",
                tx_id=f"bint:{fresh.deposit_id}:{bucket}",
                actor=fresh.holder_account_id,
                correlation_id=fresh.deposit_id,
                gross=interest,
                net=interest,
            )
            if not posted.ok:
                raise AimarketError(posted.code, posted.message)
            self.repo.record_accrual(fresh.deposit_id, bucket, interest, hours)
            self.repo.update_deposit(
                fresh.deposit_id,
                {
                    "accrued_minor": fresh.accrued_minor + interest,
                    "last_accrual_ms": fresh.last_accrual_ms + hours * HOUR_MS,
                    "version": fresh.version + 1,
                },
            )
        return interest

    # ==================== 到期处理 ====================

    def process_maturity(self, *, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """把到期的定期/理财转成活期，返回需要通知的清单。"""
        current = int(now if now is not None else now_ms())
        matured = self.repo.matured_fixed_deposits(current)
        results: List[Dict[str, Any]] = []
        for deposit in matured:
            self.accrue_one(deposit, now=current)  # 先把利息结到到期时刻
            fresh = self.repo.get_deposit(deposit.deposit_id)
            if fresh is None:
                continue
            self.repo.update_deposit(
                fresh.deposit_id,
                {
                    "kind": "demand",
                    "locked_until_ms": 0,
                    "rate_bps_per_day": self.demand_rate_bps(),
                    "version": fresh.version + 1,
                },
            )
            results.append({
                "holder_account_id": fresh.holder_account_id,
                "deposit_id": fresh.deposit_id,
                "total_minor": fresh.total_minor,
            })
        if results and self.log:
            self.log.info(f"{len(results)} 笔定期存款到期，已转入活期")
        return results
