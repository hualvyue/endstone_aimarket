"""贷款业务：放贷、计息、还款、违约判定、追债。

追债的安全要点
------------------------------------------------------------------
背包操作不可回滚，所以追债的顺序刻意排成：

    ① take 物品  →  ② 立刻把「拿到了什么」落库  →  ③ 才做变卖与抵债结算

第 ② 步单独提交，先于任何资金动作。这样即使 ③ 之前崩溃，物品也完整地记在
`repossessed_items` 里，可以重新结算 —— 绝不会出现"物品没了、欠款也没减"。

没有官方定价的物品**不销毁**，标记为 stored 留着，日后有价再卖或还清后退回。
变卖所得超出欠款的部分**退还原主**，不侵占。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import AccountKind, AccountStatus, LoanStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7
from ..core.money import format_amount
from ..core.result import Result
from ..economy.models import Entry, ROLE_PAYEE, ROLE_PAYER
from ..economy.repository import TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from ..inventory import item_codec
from .models import HOUR_MS, Loan, SeizeReport
from .repository import LoanRepository, now_ms

# 贷款投放/回收的对手方：系统账户。它允许透支，负数代表已投放的货币量。
LENDER_ACCOUNT_ID = "sys:mint"


class LoanService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        inventory=None,
        pricing=None,
        company_service=None,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.inventory = inventory
        self.pricing = pricing
        self.companies = company_service
        self.config = config
        self.log = log
        self.repo = LoanRepository(db)

    # ==================== 配置 ====================

    def _cfg_int(self, path: str, default: int) -> int:
        if self.config is None:
            return default
        try:
            return int(self.config.as_int(path, default))
        except Exception:
            return default

    def personal_base_limit_minor(self) -> int:
        return self._cfg_int("loan.personal_base_limit_minor", 100000)

    def company_limit_multiplier_bps(self) -> int:
        return self._cfg_int("loan.company_nav_multiplier_bps", 5000)

    def personal_rate_bps_per_day(self) -> int:
        return self._cfg_int("loan.personal_rate_bps_per_day", 50)   # 0.5%/天

    def company_rate_bps_per_day(self) -> int:
        return self._cfg_int("loan.company_rate_bps_per_day", 30)    # 0.3%/天

    def default_term_days(self) -> int:
        return self._cfg_int("loan.default_term_days", 7)

    def grace_days(self) -> int:
        return self._cfg_int("loan.grace_days", 3)

    def over_limit_threshold_minor(self) -> int:
        """欠款达到这个金额就直接进入追债（不等到期）。"""
        return self._cfg_int("loan.seize_threshold_minor", 500000)

    def max_total_outstanding_minor(self) -> int:
        """全局未偿总额上限，给系统增发上一道刹车。"""
        return self._cfg_int("loan.max_total_outstanding_minor", 100000000)

    # ==================== 额度 ====================

    def credit_limit(self, account_id: str) -> int:
        """可借额度。

        个人：固定上限（个人没有可抵押的资产，只能用额度天花板兜住风险）。
        公司：成立在真实资产上 —— 净资产 × 倍数，净资产越高越能借，
              资不抵债时额度自然归零，不需要额外的风控规则。
        """
        account = self.economy.repo.get_account(account_id)
        if account is None:
            return 0
        if account.kind == AccountKind.COMPANY.value:
            if self.companies is None:
                return 0
            company = self.companies.repo.get_by_account(account_id)
            if company is None:
                return 0
            nav = self.companies.nav_minor(company)
            if nav <= 0:
                return 0
            return max(0, nav * self.company_limit_multiplier_bps() // 10000)
        return self.personal_base_limit_minor()

    def available_credit(self, account_id: str) -> int:
        return max(0, self.credit_limit(account_id) - self.repo.total_debt_of(account_id))

    def global_outstanding(self) -> int:
        row = self.db.query_one(
            """SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor),0) AS debt
                 FROM loans WHERE status IN ('active','overdue','collecting')"""
        )
        return int((row or {}).get("debt") or 0)

    # ==================== 放贷 ====================

    def borrow(
        self,
        *,
        borrower_account_id: str,
        amount_minor: int,
        term_days: Optional[int] = None,
        reason: Optional[str] = None,
        rate_bps_per_day: Optional[int] = None,
        lender_account_id: Optional[str] = None,
        lender_kind: str = "bank",
        product: str = "loan",
        interest_start_ms: int = 0,
        skip_credit_limit: bool = False,
    ) -> Result[Loan]:
        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        account = self.economy.repo.get_account(borrower_account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if account.status != AccountStatus.ACTIVE.value:
            return Result.failure(ErrorCode.ACCOUNT_FROZEN, "该账户当前无法借款")

        is_player_lent = lender_kind == "player" and bool(lender_account_id)
        if is_player_lent and lender_account_id == borrower_account_id:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "不能给自己出资")

        # 银行放贷一次只能有一笔未结清（否则额度限制形同虚设）。
        # **只约束普通贷款** —— 透支、信用卡、按揭是不同产品，同时存在是正常的。
        # 玩家出资的融资同样不受此限：那是不同的债权人，公司同时欠多个人很正常。
        if not is_player_lent and product == "loan":
            existing = [
                l for l in self.repo.open_loans_of(borrower_account_id)
                if not l.is_player_lent and l.product == "loan"
            ]
            if existing:
                return Result.failure(
                    ErrorCode.DUPLICATE_TX, "你还有未结清的银行贷款，请先还清再申请"
                )

        # 玩家出资：钱从出资方账户出，得先确认他有钱
        if is_player_lent:
            lender = self.economy.repo.get_account(lender_account_id)
            if lender is None:
                return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND, "出资方账户不存在")
            if lender.status != AccountStatus.ACTIVE.value:
                return Result.failure(ErrorCode.ACCOUNT_FROZEN, "出资方账户当前不可用")
            if lender.available_minor < amount:
                return Result.failure(
                    ErrorCode.INSUFFICIENT_FUNDS,
                    f"你的可用余额不足，需要 {format_amount(amount)}，"
                    f"当前可用 {format_amount(lender.available_minor)}",
                )

        # 额度与总量上限只约束**银行放贷** —— 它们的作用是给系统增发踩刹车。
        # 玩家融资的钱来自出资玩家本人，不增发一毛钱，因此不该被这套风控挡住；
        # 它的风险由出资人自己承担，另外还有募资那一层的闸门（NAV 挂钩等）兜着。
        if not is_player_lent:
            # 央行信贷产品（透支/信用卡/按揭/再贴现）的额度由各自的产品规则管，
            # 这里跳过通用额度；但**全局放贷上限依然生效** —— 它是给货币增发踩的刹车。
            if not skip_credit_limit:
                available = self.available_credit(borrower_account_id)
                if amount > available:
                    return Result.failure(
                        ErrorCode.SUPPLY_LIMIT,
                        f"超出可借额度，最多还能借 {format_amount(available)}",
                    )

            headroom = self.max_total_outstanding_minor() - self.global_outstanding()
            if amount > headroom:
                return Result.failure(
                    ErrorCode.SUPPLY_LIMIT, "服务器当前放贷总额已达上限，请稍后再试"
                )

        is_company = account.kind == AccountKind.COMPANY.value
        if rate_bps_per_day is not None:
            rate = max(0, int(rate_bps_per_day))
        elif is_company:
            rate = self.company_rate_bps_per_day()
        else:
            rate = self.personal_rate_bps_per_day()
        term = max(1, int(term_days or self.default_term_days()))

        # 资金流向：银行放贷 = 系统增发；玩家出资 = 从他的账户真的划过来
        source_account = lender_account_id if is_player_lent else LENDER_ACCOUNT_ID
        source_role = "lender" if is_player_lent else "source"

        try:
            with self.db.transaction():
                disbursed = self.economy.post_in_transaction(
                    kind=TxKind.LOAN_DISBURSE.value,
                    entries=[
                        Entry(source_account, source_role, -amount),
                        Entry(borrower_account_id, ROLE_PAYEE, amount),
                    ],
                    idem=f"loan-disburse:{borrower_account_id}:{new_uuid7()}",
                    tx_id=f"loan:{new_uuid7()}",
                    actor=lender_account_id or borrower_account_id,
                    correlation_id=reason or "loan",
                    gross=amount,
                    net=amount,
                )
                if not disbursed.ok:
                    raise AimarketError(disbursed.code, disbursed.message)

                loan = self.repo.create(
                    borrower_account_id=borrower_account_id,
                    principal_minor=amount,
                    rate_bps_per_day=rate,
                    term_days=term,
                    is_company=is_company,
                    credit_limit_minor=self.credit_limit(borrower_account_id),
                    grace_days=self.grace_days(),
                    reason=reason,
                    lender_account_id=lender_account_id if is_player_lent else None,
                    lender_kind="player" if is_player_lent else "bank",
                    product=product,
                    interest_start_ms=int(interest_start_ms),
                )
                if loan is None:
                    raise AimarketError(ErrorCode.STORAGE_ERROR, "创建贷款记录失败")
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        if self.log:
            who = "玩家出资" if is_player_lent else "银行放贷"
            self.log.info(
                f"{who} {format_amount(amount)} 给 {account.current_name or borrower_account_id}"
                f"（{term} 天，日息 {rate / 100:.2f}%）"
            )
        return Result.success(loan)

    # ==================== 计息 ====================

    def accrue_interest(self, loan: Loan, *, now: Optional[int] = None) -> int:
        """按整小时计提简单利息。幂等靠 last_accrual_ms 在事务内推进。"""
        current = int(now if now is not None else now_ms())
        with self.db.transaction():
            fresh = self.repo.get(loan.loan_id)
            if fresh is None or not fresh.is_open:
                return 0
            if fresh.outstanding_principal_minor <= 0:
                self.repo.update(fresh.loan_id, {"last_accrual_ms": current})
                return 0

            # 免息期：interest_start_ms 之前不计息（信用卡的账单免息期靠它实现）。
            # 普通贷款 interest_start_ms = 0，于是等同于从放款时刻起算。
            start = fresh.interest_from_ms()
            from_ms = max(fresh.last_accrual_ms, start)
            if current < from_ms:
                return 0
            hours = (current - from_ms) // HOUR_MS
            if hours <= 0:
                return 0

            interest = (
                fresh.outstanding_principal_minor
                * fresh.rate_bps_per_day
                * hours
                // (10000 * 24)
            )
            # 审计记录（同一时间桶只写一次，重复执行不会双记）
            self.repo.accrue(
                loan_id=fresh.loan_id,
                bucket_ms=from_ms,
                interest_delta_minor=interest,
            )
            self.repo.update(
                fresh.loan_id,
                {
                    "accrued_interest_minor": fresh.accrued_interest_minor + max(0, interest),
                    "last_accrual_ms": from_ms + hours * HOUR_MS,
                    "version": fresh.version + 1,
                },
            )
        return max(0, interest)

    def accrue_all(self, *, now: Optional[int] = None) -> Dict[str, int]:
        total = 0
        for loan in self.repo.all_open():
            total += self.accrue_interest(loan, now=now)
        return {"accrued_minor": total}

    # ==================== 还款 ====================

    def repay(
        self,
        *,
        loan_id: str,
        amount_minor: int,
        source: str = "manual",
        allow_partial: bool = True,
    ) -> Result[Dict[str, Any]]:
        loan = self.repo.get(loan_id)
        if loan is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND)
        if not loan.is_open:
            return Result.failure(ErrorCode.TX_STATE, "这笔贷款已经结清")

        account = self.economy.repo.get_account(loan.borrower_account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)

        available = account.available_minor
        due = loan.total_due_minor
        if due <= 0:
            self.repo.set_status(loan_id, LoanStatus.SETTLED.value)
            return Result.success({"repaid_minor": 0, "settled": True})

        pay = int(amount_minor)
        if pay <= 0 or (not allow_partial and pay < due):
            return Result.failure(ErrorCode.INVALID_AMOUNT, "还款金额无效")
        pay = min(pay, due)
        if pay > available:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，需要 {format_amount(pay)}，当前可用 {format_amount(available)}",
            )

        # 先息后本
        interest_part = min(pay, loan.accrued_interest_minor)
        principal_part = pay - interest_part

        entries = [Entry(loan.borrower_account_id, ROLE_PAYER, -pay)]
        if loan.is_player_lent:
            # 玩家出资的债权：本息都要真的付给那位玩家，否则没人愿意出资
            entries.append(Entry(loan.lender_account_id, ROLE_PAYEE, pay))
        else:
            if principal_part > 0:
                entries.append(Entry(LENDER_ACCOUNT_ID, "principal", principal_part))
            if interest_part > 0:
                # 银行债权的利息进国库：既是财政收入，也天然回收了一部分投放的货币
                entries.append(Entry(TREASURY_ACCOUNT_ID, "interest", interest_part))

        try:
            with self.db.transaction():
                receipt = self.economy.post_in_transaction(
                    kind=TxKind.LOAN_REPAY.value,
                    entries=entries,
                    idem=f"loan-repay:{loan_id}:{new_uuid7()}",
                    tx_id=f"repay:{loan_id}:{new_uuid7()}",
                    actor=loan.borrower_account_id,
                    correlation_id=loan_id,
                    gross=pay,
                    net=pay - interest_part,
                    tax=interest_part,
                )
                if not receipt.ok:
                    raise AimarketError(receipt.code, receipt.message)

                new_principal = max(0, loan.outstanding_principal_minor - principal_part)
                new_interest = max(0, loan.accrued_interest_minor - interest_part)
                settled = new_principal == 0 and new_interest == 0
                self.repo.update(
                    loan_id,
                    {
                        "outstanding_principal_minor": new_principal,
                        "accrued_interest_minor": new_interest,
                        "paid_interest_minor": loan.paid_interest_minor + interest_part,
                        "status": LoanStatus.SETTLED.value if settled else loan.status,
                        "version": loan.version + 1,
                    },
                )
                self.repo.add_repayment(
                    loan_id=loan_id,
                    amount_minor=pay,
                    principal_part_minor=principal_part,
                    interest_part_minor=interest_part,
                    ledger_tx_id=receipt.value.tx_id,
                    source=source,
                )
                if settled:
                    # 贷款结清：把当初没收但没卖出去的物品还给原主
                    self._return_stored_items(loan_id)
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        return Result.success(
            {
                "repaid_minor": pay,
                "principal_part_minor": principal_part,
                "interest_part_minor": interest_part,
                "settled": settled,
                "remaining_minor": max(0, new_principal + new_interest),
            }
        )

    def repay_full(self, loan_id: str, *, source: str = "manual") -> Result[Dict[str, Any]]:
        loan = self.repo.get(loan_id)
        if loan is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND)
        return self.repay(loan_id=loan_id, amount_minor=loan.total_due_minor, source=source)

    # ==================== 违约判定 ====================

    def evaluate_default(self, loan: Loan, *, now: Optional[int] = None) -> Optional[Dict[str, str]]:
        """判断这笔贷款是否该进入追债。返回触发原因，未触发返回 None。

        触发条件（任一满足即触发，这是需求里明确要求的"逾期或超额都触发"）：
          * 逾期：超过宽限期仍未还清
          * 超额：欠款总额达到配置的追债阈值
        """
        if not loan.is_open:
            return None
        current = int(now if now is not None else now_ms())
        due = loan.total_due_minor
        if due <= 0:
            return None

        threshold = self.over_limit_threshold_minor()
        if threshold > 0 and due >= threshold:
            return {"kind": "over_limit", "reason": f"欠款达到 {format_amount(due)}，触发追债"}

        if current > loan.grace_until_ms:
            return {
                "kind": "overdue",
                "reason": f"逾期超过宽限期（到期 {format_amount(loan.principal_minor)} 未还清）",
            }

        # 宽限期内只标记逾期，不动手
        if current > loan.due_at_ms and loan.status == LoanStatus.ACTIVE.value:
            self.repo.set_status(loan.loan_id, LoanStatus.OVERDUE.value)
        return None

    def mark_collecting(self, loan: Loan, trigger: Dict[str, str]) -> None:
        self.repo.set_status(loan.loan_id, LoanStatus.COLLECTING.value)
        self.repo.add_default(
            loan_id=loan.loan_id,
            reason=trigger.get("reason", ""),
            trigger_kind=trigger.get("kind", ""),
            snapshot={
                "outstanding_principal_minor": loan.outstanding_principal_minor,
                "accrued_interest_minor": loan.accrued_interest_minor,
                "due_at_ms": loan.due_at_ms,
                "grace_until_ms": loan.grace_until_ms,
            },
        )
        if self.log:
            self.log.warning(
                f"贷款 {loan.loan_id} 进入追债（{trigger.get('kind')}）：{trigger.get('reason')}"
            )

    # ==================== 追债 ====================

    def seize(self, player, loan: Loan, *, reason: str = "collecting") -> Result[SeizeReport]:
        """没收背包物品并变卖抵债。

        顺序：take → 立刻落库 → 变卖结算。见模块顶部说明。
        """
        if self.inventory is None:
            return Result.failure(ErrorCode.INTERNAL, "背包服务不可用")

        report = SeizeReport(loan_id=loan.loan_id, borrower_account_id=loan.borrower_account_id)

        # ① 取走全部物品
        try:
            items = self.inventory.get_items(player, include_armor=False)
        except Exception as e:
            return Result.failure(ErrorCode.INTERNAL, f"读取背包失败: {e}")

        if not items:
            report.remaining_debt_minor = self.repo.get(loan.loan_id).total_due_minor
            return Result.success(report)

        taken: List[dict] = []
        for info in items:
            qty = int(info.get("count", 0) or 0)
            if qty <= 0:
                continue
            # 用完整 item_info 匹配，必然能精确取走自己的东西
            result = self.inventory.take(player, info, qty)
            actual = int(result.value or 0) if result.ok else 0
            if actual <= 0:
                continue

            payload = item_codec.with_count(info, actual)
            unit = self._sell_price(info.get("type"))
            est = actual * unit

            # ② 立刻落库（独立提交）。这一步之后即使崩溃，物品也不会丢。
            try:
                uid = self.repo.store_repossessed(
                    loan_id=loan.loan_id,
                    item_json=json.dumps(payload, ensure_ascii=False),
                    qty=actual,
                    est_value_minor=est,
                    status="stored",
                )
            except Exception as e:
                # 落库失败也要如实报出，绝不静默吞物
                if self.log:
                    self.log.error(f"没收物品落库失败（物品已取出）: {e}")
                self._give_back(player, payload)
                continue

            taken.append({"uid": uid, "payload": payload, "qty": actual, "unit": unit, "est": est})
            report.items_taken += actual

        if not taken:
            report.remaining_debt_minor = self.repo.get(loan.loan_id).total_due_minor
            return Result.success(report)

        # ③ 变卖 + 抵债结算（单个事务）
        try:
            with self.db.transaction():
                proceeds = 0
                for record in taken:
                    if record["unit"] > 0:
                        proceeds += record["est"]
                        self.repo.mark_repossessed(
                            record["uid"], status="sold", sold_price_minor=record["est"]
                        )
                        report.items_sold += record["qty"]
                    else:
                        # 没有官方定价 → 不销毁，留着
                        report.items_stored += record["qty"]
                report.proceeds_minor = proceeds

                if proceeds > 0:
                    # 变卖：系统按回收价收走物品并付钱（等价于一次铸币）
                    realized = self.economy.post_in_transaction(
                        kind=TxKind.LOAN_SEIZE.value,
                        entries=[
                            Entry(LENDER_ACCOUNT_ID, "source", -proceeds),
                            Entry(loan.borrower_account_id, ROLE_PAYEE, proceeds),
                        ],
                        idem=f"seize-proceeds:{loan.loan_id}:{new_uuid7()}",
                        tx_id=f"seize:{loan.loan_id}:{new_uuid7()}",
                        actor=loan.borrower_account_id,
                        correlation_id=loan.loan_id,
                        gross=proceeds,
                        net=proceeds,
                    )
                    if not realized.ok:
                        raise AimarketError(realized.code, realized.message)

            # 用变卖所得还款（走标准还款路径，会计处理与手动还款完全一致）
            if proceeds > 0:
                repaid = self.repay(
                    loan_id=loan.loan_id, amount_minor=proceeds, source="seize"
                )
                if repaid.ok:
                    report.repaid_minor = repaid.value.get("repaid_minor", 0)
                    report.refunded_minor = max(0, proceeds - report.repaid_minor)
                    report.settled = bool(repaid.value.get("settled"))
                else:
                    report.messages.append(f"抵债失败: {repaid.message}")
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        fresh = self.repo.get(loan.loan_id)
        report.remaining_debt_minor = fresh.total_due_minor if fresh else 0
        # 贷款刚好在这次追债中结清 → 立刻把暂存的无价物品还回去，别让它躺在库里
        if report.settled:
            returned = self.deliver_returns(player, loan.borrower_account_id)
            if returned:
                report.messages.append(f"已归还暂存物品 {returned} 件")
        if self.log:
            self.log.warning(f"追债 {loan.loan_id}: {report.describe()}")
        return Result.success(report)

    def _give_back(self, player, payload: Dict[str, Any]) -> bool:
        if self.inventory is None:
            return False
        try:
            return self.inventory.give(player, payload, int(payload.get("count", 0))).ok
        except Exception:
            return False

    def _return_stored_items(self, loan_id: str) -> int:
        """贷款结清后，把当初没收但没能卖出去的物品标记为可归还。"""
        items = self.repo.repossessed_of(loan_id, status="stored")
        for item in items:
            self.repo.mark_repossessed(item.item_uid, status="returned")
        return len(items)

    def pending_return_items(self, loan_id: str) -> List[dict]:
        """给上层用：结清后尚未真正交还玩家的物品。"""
        return [
            {"uid": i.item_uid, "item": json.loads(i.item_json), "qty": i.qty}
            for i in self.repo.repossessed_of(loan_id, status="returned")
        ]

    def deliver_returns(self, player, account_id: str) -> int:
        """把「没收时因无官方定价而暂存、贷款结清后标记归还」的物品真正交回玩家。

        玩家离线时不做，等他上线由 collector 补交 —— 物品在库里躺着，不会丢。
        """
        if self.inventory is None:
            return 0
        rows = self.db.query_all(
            """SELECT r.* FROM repossessed_items r
                 JOIN loans l ON l.loan_id = r.loan_id
                WHERE l.borrower_account_id = ? AND r.status = 'returned'""",
            (account_id,),
        )
        delivered = 0
        for row in rows:
            try:
                info = json.loads(row["item_json"])
                qty = int(row["qty"])
                result = self.inventory.give(player, info, qty)
                if not result.ok or int(result.value or 0) <= 0:
                    continue
                got = int(result.value)
                delivered += got
                if got >= qty:
                    self.db.delete("repossessed_items", "item_uid = ?", (row["item_uid"],))
                else:
                    self.db.update(
                        "repossessed_items",
                        {"qty": qty - got, "updated_at_ms": now_ms()},
                        "item_uid = ?",
                        (row["item_uid"],),
                    )
            except Exception as e:
                if self.log:
                    self.log.error(f"归还物品失败 {row.get('item_uid')}: {e}")
        if delivered and self.log:
            self.log.info(f"已向玩家归还 {delivered} 件暂存物品")
        return delivered

    def _sell_price(self, item_type: Optional[str]) -> int:
        if not item_type or self.pricing is None:
            return 0
        try:
            return int(self.pricing.quote_price(item_type, "sell") or 0)
        except Exception:
            return 0

    # ==================== 查询 ====================

    def loans_of(self, account_id: str) -> List[Loan]:
        return self.repo.open_loans_of(account_id)

    def get(self, loan_id: str) -> Optional[Loan]:
        return self.repo.get(loan_id)

    def total_debt_of(self, account_id: str) -> int:
        return self.repo.total_debt_of(account_id)

    def stats(self) -> Dict[str, int]:
        return self.repo.stats()

    def describe(self, loan: Loan) -> str:
        return (
            f"本金 {format_amount(loan.outstanding_principal_minor)} "
            f"+ 利息 {format_amount(loan.accrued_interest_minor)} "
            f"= 应付 {format_amount(loan.total_due_minor)}"
        )
