"""央行的信贷业务：透支 / 信用卡 / 按揭 / 再贴现。

**统一的骨架**：所有信贷都会在 `loans` 表产生一笔记录（靠 `product` 区分），
于是计息、逾期判定、追债（没收背包 + 变卖）、坏账核销、清算受偿**全部复用贷款模块**，
不必把这些容易出错的逻辑重写一遍。产品特有的元数据（额度、账单、抵押物）
放在各自的表里，通过 `loan_id` 指回来。

**资金流向统一为"央行印钞"**：`loans.borrow` 的分录就是
`发行科目 −X / 借款人 +X`，也就是央行创造了这笔钱。债权记在 loans 表里，
于是资产负债表自动自洽（负债 +X、资产 +X、资本不变）。

### 透支为什么不是"余额变负"

数据库有硬约束 `CHECK (balance_minor >= 0 OR kind = 'system')`，玩家账户永远不能为负。
所以透支做成 **「授信额度 + 自动放款」**：付款时余额不足，就从额度里借一笔补上，
玩家感受到的是"能花超"，物理上账户余额恒为正，**完全不触碰那条约束**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import CreditStatus, MortgageStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.money import format_amount
from ..core.result import Result
from ..economy.service import EconomyService
from ..loan.service import LoanService
from .models import Card, CreditLine, Mortgage, MortgageSchedule, Statement
from .repository import DAY_MS, HOUR_MS, now_ms
from .service import CentralBankService


class LendingService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        bank: CentralBankService,
        loans: LoanService,
        *,
        inventory=None,
        pricing=None,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.bank = bank
        self.loans = loans
        self.inventory = inventory
        self.pricing = pricing
        self.config = config
        self.log = log
        self.repo = bank.repo
        self.loan_repo = loans.repo

    # ==================== 配置 ====================

    def _cfg_int(self, path: str, default: int) -> int:
        if self.config is None:
            return default
        return int(self.config.as_int(path, default) or default)

    def _cfg_bool(self, path: str, default: bool) -> bool:
        if self.config is None:
            return default
        return bool(self.config.get(path, default))

    def auto_overdraft_enabled(self) -> bool:
        return self._cfg_bool("bank.auto_overdraft", True)

    def overdraft_term_days(self) -> int:
        return max(1, self._cfg_int("bank.overdraft_term_days", 7))

    def card_term_days(self) -> int:
        return max(1, self._cfg_int("bank.card_term_days", 30))

    def card_cycle_days(self) -> int:
        return max(1, self._cfg_int("bank.card_cycle_days", 7))

    def card_grace_days(self) -> int:
        return max(0, self._cfg_int("bank.card_grace_days", 7))

    def card_min_pay_bps(self) -> int:
        return max(0, self._cfg_int("bank.card_min_pay_bps", 1000))

    def discount_term_days(self) -> int:
        return max(1, self._cfg_int("bank.discount_term_days", 7))

    def discount_haircut_bps(self) -> int:
        return max(0, min(10000, self._cfg_int("bank.discount_haircut_bps", 4000)))

    def mortgage_haircut_bps(self) -> int:
        return max(0, min(10000, self._cfg_int("bank.mortgage_haircut_bps", 3000)))

    def mortgage_max_periods(self) -> int:
        return max(1, self._cfg_int("bank.mortgage_max_periods", 12))

    # ==================== 内部工具 ====================

    def _disburse(
        self,
        account_id: str,
        amount: int,
        *,
        product: str,
        rate_bps: int,
        term_days: int,
        interest_start_ms: int = 0,
        reason: str = "",
    ) -> Result:
        """统一放款入口：先过央行闸门（资本充足率 + 准备金上限），再记账。"""
        guard = self.bank.can_lend(amount)
        if not guard.ok:
            return guard
        return self.loans.borrow(
            borrower_account_id=account_id,
            amount_minor=amount,
            term_days=term_days,
            rate_bps_per_day=rate_bps,
            product=product,
            interest_start_ms=interest_start_ms,
            skip_credit_limit=True,
            reason=reason,
        )

    def _outstanding(self, product: str, account_id: str) -> int:
        """余额一律从 loans 派生，不做冗余字段 —— 天然不会与债务不同步。"""
        return self.loan_repo.outstanding_of_product(product, borrower_account_id=account_id)

    def _open_loans(self, product: str, account_id: str):
        return self.loan_repo.open_loans_of_product(account_id, product)

    # ==================== 透支（授信额度 + 自动放款） ====================

    def overdraft_limit_minor(self, account_id: str) -> int:
        base = self._cfg_int("bank.overdraft_base_limit_minor", 100000)
        mult_bps = self._cfg_int("bank.overdraft_balance_multiplier_bps", 0)
        if mult_bps <= 0:
            return max(0, base)
        balance = self.economy.get_balance(account_id).balance_minor
        return max(base, balance * mult_bps // 10000)

    def open_overdraft(self, account_id: str) -> Result[CreditLine]:
        existing = self.repo.get_credit_line(account_id)
        if existing is not None and existing.status == CreditStatus.ACTIVE.value:
            return Result.success(existing)
        account = self.economy.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        limit = self.overdraft_limit_minor(account_id)
        if limit <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "当前额度为 0，无法开通透支")
        rate = self.bank.policy().overdraft_rate_bps()
        with self.db.transaction():
            line = self.repo.create_credit_line(
                holder_account_id=account_id,
                credit_limit_minor=limit,
                rate_bps_per_day=rate,
                due_at_ms=now_ms() + self.overdraft_term_days() * DAY_MS,
            )
        return Result.success(line)

    def credit_line_of(self, account_id: str) -> Optional[CreditLine]:
        return self.repo.get_credit_line(account_id)

    def overdraft_available(self, account_id: str) -> int:
        line = self.repo.get_credit_line(account_id)
        if line is None or line.status != CreditStatus.ACTIVE.value:
            return 0
        used = self._outstanding("overdraft", account_id)
        return max(0, line.credit_limit_minor - used)

    def overdraft_used(self, account_id: str) -> int:
        return self._outstanding("overdraft", account_id)

    def draw_overdraft(self, account_id: str, amount: int) -> Result:
        """真的从额度里借钱（钱进玩家账户）。"""
        amount = int(amount)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        line = self.repo.get_credit_line(account_id)
        if line is None or line.status != CreditStatus.ACTIVE.value:
            return Result.failure(ErrorCode.BANK_CARD_MISSING, "你还没有开通透支")
        available = self.overdraft_available(account_id)
        if amount > available:
            return Result.failure(
                ErrorCode.BANK_CREDIT_LIMIT,
                f"超出透支额度，还能用 {format_amount(available)}",
            )
        rate = self.bank.policy().overdraft_rate_bps()
        return self._disburse(
            account_id, amount,
            product="overdraft",
            rate_bps=rate,
            term_days=self.overdraft_term_days(),
            reason="overdraft",
        )

    def ensure_funds(self, account_id: str, amount: int) -> Result[int]:
        """支付前钩子：余额不足就从透支额度自动补足。返回补足的金额。

        这是"透支体验"的核心 —— 玩家永远是先付款成功，再意识到自己透支了。
        """
        amount = int(amount)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if not self.auto_overdraft_enabled():
            return Result.failure(ErrorCode.INSUFFICIENT_FUNDS)
        balance = self.economy.get_balance(account_id).available_minor
        if balance >= amount:
            return Result.success(0)
        gap = amount - balance
        if self.overdraft_available(account_id) < gap:
            return Result.failure(ErrorCode.INSUFFICIENT_FUNDS)
        drawn = self.draw_overdraft(account_id, gap)
        if not drawn.ok:
            return Result.failure(drawn.code, drawn.message)
        return Result.success(gap)

    def repay_overdraft(self, account_id: str, amount: int, *, source: str = "manual") -> Result[Dict[str, Any]]:
        """还款：按到期日顺序逐笔结清。"""
        return self._repay_product("overdraft", account_id, amount, source=source)

    # ==================== 信用卡 ====================

    def card_limit_minor(self, account_id: str) -> int:
        base = self._cfg_int("bank.card_base_limit_minor", 300000)
        mult_bps = self._cfg_int("bank.card_balance_multiplier_bps", 0)
        if mult_bps <= 0:
            return max(0, base)
        points = self._credit_history_minor(account_id)
        return max(base, points * mult_bps // 10000)

    def _credit_history_minor(self, account_id: str) -> int:
        """信用记录：累计还款额，作为提额依据。"""
        value = self.db.scalar(
            "SELECT COALESCE(SUM(paid_interest_minor), 0) FROM loans "
            "WHERE borrower_account_id = ? AND product IN ('card','overdraft')",
            (account_id,),
            default=0,
        )
        return int(value or 0)

    def issue_card(self, account_id: str, *, limit_minor: Optional[int] = None) -> Result[Card]:
        existing = self.repo.cards_of(account_id, status=CreditStatus.ACTIVE.value)
        if existing:
            return Result.failure(ErrorCode.ACCOUNT_EXISTS, "你已经有一张信用卡了")
        account = self.economy.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        limit = int(limit_minor) if limit_minor else self.card_limit_minor(account_id)
        if limit <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "额度必须大于 0")
        policy = self.bank.policy()
        now = now_ms()
        with self.db.transaction():
            card = self.repo.create_card(
                holder_account_id=account_id,
                credit_limit_minor=limit,
                apr_bps_per_day=policy.card_rate_bps(),
                penalty_bps_per_day=policy.card_rate_bps() * 15 // 10,
                cycle_days=self.card_cycle_days(),
                min_pay_bps=self.card_min_pay_bps(),
                grace_days=self.card_grace_days(),
                next_statement_ms=now + self.card_cycle_days() * DAY_MS,
            )
        return Result.success(card)

    def card_of(self, account_id: str) -> Optional[Card]:
        cards = self.repo.cards_of(account_id, status=CreditStatus.ACTIVE.value)
        return cards[0] if cards else None

    def card_used(self, account_id: str) -> int:
        return self._outstanding("card", account_id)

    def card_available(self, account_id: str) -> int:
        card = self.card_of(account_id)
        if card is None:
            return 0
        return max(0, card.credit_limit_minor - self.card_used(account_id))

    def card_charge(self, account_id: str, amount: int, *, payee_account_id: Optional[str] = None) -> Result:
        """刷卡消费。免息期靠 `interest_start_ms` 实现 —— 到期才开始计息。"""
        amount = int(amount)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        card = self.card_of(account_id)
        if card is None:
            return Result.failure(ErrorCode.BANK_CARD_MISSING, "你还没有信用卡")
        available = self.card_available(account_id)
        if amount > available:
            return Result.failure(
                ErrorCode.BANK_CREDIT_LIMIT,
                f"超出信用额度，还能刷 {format_amount(available)}",
            )
        # 免息期：账单周期 + 宽限天数
        interest_start = now_ms() + (card.cycle_days + card.grace_days) * DAY_MS
        result = self._disburse(
            account_id, amount,
            product="card",
            rate_bps=card.apr_bps_per_day,
            term_days=self.card_term_days(),
            interest_start_ms=interest_start,
            reason="card_charge",
        )
        if not result.ok:
            return result
        if payee_account_id:
            moved = self.economy.transfer(
                account_id, payee_account_id, amount,
                reason="card_spend", idem=f"card-spend:{result.value.loan_id}",
            )
            if not moved.ok:
                # 收款失败就把刚放出的款还掉（同一事务保证不会出现悬空债务）
                return Result.failure(moved.code, moved.message)
        return result

    def run_statements(self, *, now: Optional[int] = None) -> List[Statement]:
        """出账单：把本期消费汇总成一条账单，供玩家查看与最低还款提醒。

        注意真正的计息由每笔消费的 `interest_start_ms` 控制，账单只负责**展示与催收**。
        """
        current = int(now if now is not None else now_ms())
        cards = self.repo.cards_due_statement(current)
        out: List[Statement] = []
        for card in cards:
            used = self.card_used(card.holder_account_id)
            if used > 0:
                min_pay = used * card.min_pay_bps // 10000
                due = current + (card.grace_days + card.cycle_days) * DAY_MS
                with self.db.transaction():
                    stmt = self.repo.create_statement(
                        card_id=card.card_id,
                        period_end_ms=current,
                        due_at_ms=due,
                        amount_minor=used,
                        min_pay_minor=min_pay,
                    )
                out.append(stmt)
            self.repo.update_card(
                card.card_id,
                {"next_statement_ms": current + card.cycle_days * DAY_MS},
            )
        if out and self.log:
            self.log.info(f"已生成 {len(out)} 份信用卡账单")
        return out

    def card_pay(self, account_id: str, amount: int, *, source: str = "manual") -> Result[Dict[str, Any]]:
        result = self._repay_product("card", account_id, amount, source=source)
        if result.ok:
            # 还款后把账单标记为已还（账单只是展示层，这里同步一下状态）
            for stmt in self.repo.open_statements(self.card_of(account_id).card_id
                                                 if self.card_of(account_id) else ""):
                self.repo.update_statement(
                    stmt.statement_id,
                    {
                        "paid_minor": stmt.amount_minor,
                        "status": "paid",
                    },
                )
        return result

    # ==================== 按揭（实体店抵押、分期还款） ====================

    def estimate_shop(self, shop: Dict[str, Any]) -> int:
        """店铺估值 = 店内托管资金 + 库存按官方回收价。"""
        total = 0
        escrow_id = shop.get("escrow_account_id")
        if escrow_id:
            total += self.economy.get_balance(escrow_id).balance_minor
        stock_qty = int(shop.get("stock_qty") or 0)
        if stock_qty > 0 and self.pricing is not None:
            try:
                import json

                info = json.loads(shop.get("offer_item_json") or "{}")
                unit = self.pricing.quote_price(info.get("type"), "sell") or 0
                total += unit * stock_qty
            except Exception:
                pass
        # 收购店铺收到的货也算资产
        collected = shop.get("collected_json")
        if collected:
            try:
                import json

                payload = json.loads(collected)
                for item in payload.get("items", []) if isinstance(payload, dict) else []:
                    qty = int(item.get("qty", 0))
                    unit = 0
                    if self.pricing is not None:
                        unit = self.pricing.quote_price(item.get("type"), "sell") or 0
                    total += unit * qty
            except Exception:
                pass
        return max(0, total)

    def mortgage_quota(self, shop: Dict[str, Any]) -> int:
        """按揭可贷额度 = 店铺估值 × (1 − 抵押折扣)。"""
        value = self.estimate_shop(shop)
        return max(0, value * (10000 - self.mortgage_haircut_bps()) // 10000)

    def open_mortgage(
        self,
        *,
        borrower_account_id: str,
        shop: Dict[str, Any],
        amount_minor: int,
        periods: int = 6,
    ) -> Result[Mortgage]:
        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        shop_id = str(shop.get("shop_id") or "")
        if not shop_id:
            return Result.failure(ErrorCode.BANK_MORTGAGE_INVALID, "抵押物无效")
        if self.repo.collateral_in_use(shop_id):
            return Result.failure(ErrorCode.BANK_MORTGAGE_INVALID, "这家店已经抵押过了")
        value = self.estimate_shop(shop)
        quota = self.mortgage_quota(shop)
        if amount > quota:
            return Result.failure(
                ErrorCode.BANK_MORTGAGE_INVALID,
                f"超出抵押额度：店铺估值 {format_amount(value)}，最多可贷 {format_amount(quota)}",
            )
        periods = max(1, min(self.mortgage_max_periods(), int(periods)))
        rate = self.bank.policy().credit_rate_bps()

        # 分期数决定期限：每期 7 天
        term_days = periods * 7
        with self.db.transaction():
            disbursed = self._disburse(
                borrower_account_id, amount,
                product="mortgage",
                rate_bps=rate,
                term_days=term_days,
                reason=f"mortgage:{shop_id}",
            )
            if not disbursed.ok:
                return disbursed
            loan = disbursed.value
            now = now_ms()
            mortgage = self.repo.create_mortgage(
                borrower_account_id=borrower_account_id,
                collateral_kind="shop",
                collateral_ref=shop_id,
                collateral_value_minor=value,
                principal_minor=amount,
                rate_bps_per_day=rate,
                term_days=term_days,
                periods=periods,
                next_due_ms=now + 7 * DAY_MS,
                loan_id=loan.loan_id,
            )
            self.repo.create_schedules(
                mortgage.mortgage_id,
                self._build_schedules(amount, rate, periods, now),
            )
        if self.log:
            self.log.info(
                f"按揭放款 {format_amount(amount)}（抵押店铺 {shop_id}，"
                f"估值 {format_amount(value)}，{periods} 期）"
            )
        return Result.success(mortgage)

    def _build_schedules(self, principal: int, rate_bps: int, periods: int, start_ms: int) -> List[Dict[str, Any]]:
        """等额本金：每期本金相同，利息按剩余本金算。简单、可预期、不会算错。"""
        per = principal // periods
        schedules: List[Dict[str, Any]] = []
        remaining = principal
        for index in range(periods):
            this_principal = per if index < periods - 1 else remaining
            interest = remaining * rate_bps * 7 // 10000
            schedules.append({
                "period_index": index + 1,
                "due_at_ms": start_ms + (index + 1) * 7 * DAY_MS,
                "principal_minor": this_principal,
                "interest_minor": interest,
            })
            remaining -= this_principal
        return schedules

    def mortgages_of(self, account_id: str) -> List[Mortgage]:
        return self.repo.mortgages_of(account_id)

    def mortgage_schedules(self, mortgage_id: str) -> List[MortgageSchedule]:
        return self.repo.schedules_of(mortgage_id)

    def repay_mortgage(self, account_id: str, amount: int, *, source: str = "manual") -> Result[Dict[str, Any]]:
        return self._repay_product("mortgage", account_id, amount, source=source)

    def process_mortgage_due(self, *, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """到期检查：把还款计划的进度同步到实际债务上，并标记逾期。

        真正的违约追债由贷款模块统一处理（`LoanCollector`），这里只负责计划表与状态。
        """
        current = int(now if now is not None else now_ms())
        out: List[Dict[str, Any]] = []
        for mortgage in self.repo.mortgages_of_all_active():
            loans = self._open_loans("mortgage", mortgage.borrower_account_id)
            loan = next((l for l in loans if l.loan_id == mortgage.loan_id), None)
            outstanding = loan.total_due_minor if loan else 0
            self.repo.update_mortgage(
                mortgage.mortgage_id,
                {"outstanding_minor": outstanding, "version": mortgage.version + 1},
            )
            if outstanding <= 0:
                self.repo.update_mortgage(
                    mortgage.mortgage_id,
                    {"status": MortgageStatus.SETTLED.value, "version": mortgage.version + 2},
                )
                out.append({"mortgage_id": mortgage.mortgage_id, "event": "settled"})
                continue
            # 违约金/逾期：loan 自己会进入 collecting，这里同步状态
            if loan is not None and loan.is_collecting:
                self.repo.update_mortgage(
                    mortgage.mortgage_id,
                    {"status": MortgageStatus.OVERDUE.value, "version": mortgage.version + 2},
                )
                self.foreclose(mortgage, reason="default")
                out.append({"mortgage_id": mortgage.mortgage_id, "event": "foreclosed"})
        return out

    def foreclose(self, mortgage: Mortgage, *, reason: str = "default") -> Result[Dict[str, Any]]:
        """收走抵押物：关闭该实体店，所有权归央行处置。"""
        if mortgage.status == MortgageStatus.FORECLOSED.value:
            return Result.success({"shop_id": mortgage.collateral_ref, "already": True})
        shop = self.db.query_one(
            "SELECT * FROM shops WHERE shop_id = ?", (mortgage.collateral_ref,)
        )
        with self.db.transaction():
            if shop is not None and shop.get("status") == "active":
                self.db.update(
                    "shops",
                    {"status": "closed", "updated_at_ms": now_ms()},
                    "shop_id = ?",
                    (mortgage.collateral_ref,),
                )
            self.repo.update_mortgage(
                mortgage.mortgage_id,
                {"status": MortgageStatus.FORECLOSED.value, "version": mortgage.version + 1},
            )
            self.repo.add_event(
                kind="foreclose",
                amount_minor=mortgage.outstanding_minor,
                detail=f"按揭违约收店 {mortgage.collateral_ref}（{reason}）",
            )
        if self.log:
            self.log.warning(
                f"按揭违约：已收走抵押店铺 {mortgage.collateral_ref}"
            )
        return Result.success({
            "shop_id": mortgage.collateral_ref,
            "outstanding_minor": mortgage.outstanding_minor,
        })

    # ==================== 再贴现窗口 ====================

    def discount_quota(self, shop: Dict[str, Any]) -> int:
        value = self.estimate_shop(shop)
        return max(0, value * (10000 - self.discount_haircut_bps()) // 10000)

    def open_discount(
        self,
        *,
        borrower_account_id: str,
        shop: Dict[str, Any],
        amount_minor: int,
    ) -> Result:
        """再贴现：短期周转融资，抵押物同样是实体店，额度和期限都比按揭紧。"""
        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        shop_id = str(shop.get("shop_id") or "")
        if not shop_id:
            return Result.failure(ErrorCode.BANK_MORTGAGE_INVALID, "抵押物无效")
        if self.repo.collateral_in_use(shop_id):
            return Result.failure(ErrorCode.BANK_MORTGAGE_INVALID, "这家店已经抵押过了")
        quota = self.discount_quota(shop)
        if amount > quota:
            return Result.failure(
                ErrorCode.BANK_MORTGAGE_INVALID,
                f"超出抵押额度，最多 {format_amount(quota)}（抵押率 "
                f"{100 - self.discount_haircut_bps() / 100:.0f}%）",
            )
        rate = self.bank.policy().discount_rate_bps()
        return self._disburse(
            borrower_account_id, amount,
            product="discount",
            rate_bps=rate,
            term_days=self.discount_term_days(),
            reason=f"discount:{shop_id}",
        )

    # ==================== 通用还款 ====================

    def _repay_product(self, product: str, account_id: str, amount: int, *, source: str) -> Result[Dict[str, Any]]:
        """按到期日顺序逐笔还款。本金与利息分开统计，便于报表。"""
        amount = int(amount)
        loans = self._open_loans(product, account_id)
        if not loans:
            return Result.failure(ErrorCode.TX_NOT_FOUND, "你没有需要偿还的欠款")
        total_due = sum(l.total_due_minor for l in loans)
        if amount <= 0 or amount > total_due:
            amount = total_due  # 还多了就按全额算，避免多扣

        paid_principal = 0
        paid_interest = 0
        remaining = amount
        for loan in loans:
            if remaining <= 0:
                break
            part = min(remaining, loan.total_due_minor)
            result = self.loans.repay(loan_id=loan.loan_id, amount_minor=part, source=source)
            if not result.ok:
                return Result.failure(result.code, result.message)
            detail = result.value or {}
            paid_principal += int(detail.get("principal_minor", 0))
            paid_interest += int(detail.get("interest_minor", 0))
            remaining -= part

        left = self._outstanding(product, account_id)
        return Result.success({
            "paid_minor": amount - remaining,
            "principal_minor": paid_principal,
            "interest_minor": paid_interest,
            "remaining_minor": left,
            "settled": left <= 0,
        })
