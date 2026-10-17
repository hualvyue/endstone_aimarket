"""理财业务。

**本轮只落端口**：数据结构、申购/赎回/到期结算全链路可用，收益由"策略"决定。
现在内置的只有固定收益策略；后期接 AI 时，只需注册一个新的 `WealthStrategy`
实现，界面与数据表都不用动。

策略接口刻意做得和 `analytics/ai_gateway.py` 的 `AIAdvisor` 同构：
都有 `key`、`available()`、一个决策方法。这样"用 AI 决定理财配置"只是多一个类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from ..core.constants import DepositStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.money import format_amount
from ..core.result import Result
from ..economy.models import ROLE_PAYEE, ROLE_PAYER, Entry
from ..economy.service import EconomyService
from .deposit import DepositService
from .repository import DAY_MS, now_ms
from .service import CENTRAL_BANK_ISSUE_ACCOUNT_ID, CentralBankService


class WealthStrategy(ABC):
    """理财策略。决定"这笔钱到期能拿回多少"。

    返回的是**收益率（基点）**，可以是负的（亏本）—— 真实理财不保本，
    但内置的固定收益策略取正值，风险等级标为最低。
    """

    key: str = "fixed"
    name: str = "固定收益"

    @abstractmethod
    def allocate(self, *, product: Dict[str, Any], amount_minor: int,
                 term_days: int, snapshot: Any = None) -> int:
        """决定收益率（基点）。"""

    def available(self) -> bool:
        return True


class FixedRateStrategy(WealthStrategy):
    """内置策略：按产品标明的预期收益率结算。

    这是唯一不依赖任何外部信息的策略，所以它同时是兜底策略 ——
    将来 AI 策略不可用时（未配置、超时、报错）会自动回退到它。
    """

    key = "fixed"
    name = "固定收益"

    def allocate(self, *, product: Dict[str, Any], amount_minor: int,
                 term_days: int, snapshot: Any = None) -> int:
        bps = int(product.get("expected_bps") or 0)
        return max(0, bps)


class WealthService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        bank: CentralBankService,
        deposits: DepositService,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.bank = bank
        self.deposits = deposits
        self.config = config
        self.log = log
        self.repo = bank.repo
        self._strategies: Dict[str, WealthStrategy] = {}
        self.register_strategy(FixedRateStrategy())

    # ==================== 策略注册 ====================

    def register_strategy(self, strategy: WealthStrategy) -> None:
        self._strategies[strategy.key] = strategy

    def strategy(self, key: str) -> WealthStrategy:
        found = self._strategies.get(str(key))
        if found is not None and found.available():
            return found
        return self._strategies["fixed"]  # 兜底：永远有策略可用

    def strategies(self) -> List[str]:
        return sorted(self._strategies)

    # ==================== 产品 ====================

    def ensure_products(self) -> None:
        """把内置产品写进表。服主可以在 config 里覆盖参数。"""
        defaults = [
            {"product_id": "steady7", "name": "稳健 7 天", "strategy_key": "fixed",
             "risk_level": 1, "min_amount_minor": 10000, "term_days": 7, "expected_bps": 150},
            {"product_id": "steady30", "name": "稳健 30 天", "strategy_key": "fixed",
             "risk_level": 1, "min_amount_minor": 50000, "term_days": 30, "expected_bps": 800},
        ]
        for item in defaults:
            self.repo.upsert_product(**item)

    def list_products(self) -> List[Dict[str, Any]]:
        return self.repo.list_products()

    def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_product(product_id)

    # ==================== 申购 ====================

    def subscribe(self, *, account_id: str, product_id: str, amount_minor: int) -> Result[Dict[str, Any]]:
        amount = int(amount_minor)
        product = self.repo.get_product(product_id)
        if product is None or product.get("status") != "active":
            return Result.failure(ErrorCode.TX_NOT_FOUND, "没有这个理财产品")
        minimum = int(product.get("min_amount_minor") or 0)
        if amount < minimum:
            return Result.failure(
                ErrorCode.INVALID_AMOUNT, f"起投金额 {format_amount(minimum)}"
            )
        term_days = max(1, int(product.get("term_days") or 7))

        # 资金进理财专用的存款账户（复用存款那套记账，M2 能算准）
        opened = self.deposits.deposit(
            holder_account_id=account_id,
            amount_minor=amount,
            kind="wealth",
            days=term_days,
            product_id=product_id,
        )
        if not opened.ok:
            return opened
        record = opened.value

        strategy = self.strategy(str(product.get("strategy_key") or "fixed"))
        snapshot = None
        if self.config is not None:
            try:
                from ..analytics.query import MetricsQuery

                snapshot = MetricsQuery(self.db).overview()
            except Exception:
                snapshot = None
        return_bps = strategy.allocate(
            product=product, amount_minor=amount, term_days=term_days, snapshot=snapshot
        )

        with self.db.transaction():
            holding_id = self.repo.create_holding(
                product_id=product_id,
                holder_account_id=account_id,
                amount_minor=amount,
                deposit_id=record.deposit_id,
                return_bps=return_bps,
                matures_at_ms=record.locked_until_ms,
            )
        if self.log:
            self.log.info(
                f"理财申购 {format_amount(amount)} → {product.get('name')}"
                f"（预期 {return_bps / 10000:.2%}，{term_days} 天）"
            )
        return Result.success({
            "holding_id": holding_id,
            "deposit_id": record.deposit_id,
            "amount_minor": amount,
            "return_bps": return_bps,
            "term_days": term_days,
        })

    # ==================== 赎回与结算 ====================

    def redeem(self, *, account_id: str, holding_id: str, allow_early: bool = False) -> Result[Dict[str, Any]]:
        holdings = self.repo.holdings_of(account_id, status="active")
        holding = next((h for h in holdings if h["holding_id"] == holding_id), None)
        if holding is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND, "没有这笔理财")
        deposit_id = holding.get("deposit_id")
        if not deposit_id:
            return Result.failure(ErrorCode.STORAGE_ERROR, "这笔理财缺少资金账户")

        settled = self.settle(holding, allow_early=allow_early)
        if not settled.ok:
            return settled
        return Result.success(settled.value)

    def settle(self, holding: Dict[str, Any], *, allow_early: bool = False,
               now: Optional[int] = None) -> Result[Dict[str, Any]]:
        """结算一笔理财：算收益、增发/销毁差额、把钱转回玩家。"""
        current = int(now if now is not None else now_ms())
        deposit_id = holding.get("deposit_id")
        deposit = self.bank.repo.get_deposit(deposit_id) if deposit_id else None
        if deposit is None:
            return Result.failure(ErrorCode.BANK_DEPOSIT_NOT_FOUND, "资金账户不存在")
        if deposit.is_locked(current) and not allow_early:
            return Result.failure(ErrorCode.BANK_DEPOSIT_LOCKED, "还没到期")

        principal = deposit.principal_minor
        return_bps = int(holding.get("return_bps") or 0)
        target = max(0, principal * (10000 + return_bps) // 10000)
        balance = self.economy.get_balance(deposit.deposit_account_id).balance_minor
        # 已计提的存款利息也算进收益，避免重复发放
        payout = min(target, balance) if return_bps >= 0 else min(balance, target)
        diff = balance - payout

        try:
            with self.db.transaction():
                if diff > 0:
                    # 收益不足 / 亏损：多出来的部分销毁
                    burn = self.economy.post_in_transaction(
                        kind=TxKind.BANK_DEPOSIT_OUT.value,
                        entries=[
                            Entry(deposit.deposit_account_id, ROLE_PAYER, -diff),
                            Entry(CENTRAL_BANK_ISSUE_ACCOUNT_ID, "sink", diff),
                        ],
                        idem=f"wealth-burn:{holding['holding_id']}",
                        tx_id=f"wb:{holding['holding_id']}",
                        actor=holding["holder_account_id"],
                        correlation_id=holding["holding_id"],
                        gross=diff, net=diff,
                    )
                    if not burn.ok:
                        raise AimarketError(burn.code, burn.message)
                elif diff < 0:
                    # 收益高于已计提利息：央行增发补足
                    extra = -diff
                    mint = self.economy.post_in_transaction(
                        kind=TxKind.BANK_INTEREST.value,
                        entries=[
                            Entry(CENTRAL_BANK_ISSUE_ACCOUNT_ID, "source", -extra),
                            Entry(deposit.deposit_account_id, ROLE_PAYEE, extra),
                        ],
                        idem=f"wealth-mint:{holding['holding_id']}",
                        tx_id=f"wm:{holding['holding_id']}",
                        actor=holding["holder_account_id"],
                        correlation_id=holding["holding_id"],
                        gross=extra, net=extra,
                    )
                    if not mint.ok:
                        raise AimarketError(mint.code, mint.message)
                    payout = balance + extra

                jump = self.economy.post_in_transaction(
                    kind=TxKind.BANK_DEPOSIT_OUT.value,
                    entries=[
                        Entry(deposit.deposit_account_id, ROLE_PAYER, -payout),
                        Entry(holding["holder_account_id"], ROLE_PAYEE, payout),
                    ],
                    idem=f"wealth-payout:{holding['holding_id']}",
                    tx_id=f"wp:{holding['holding_id']}",
                    actor=holding["holder_account_id"],
                    correlation_id=holding["holding_id"],
                    gross=payout, net=payout,
                )
                if not jump.ok:
                    raise AimarketError(jump.code, jump.message)

                self.bank.repo.update_deposit(
                    deposit.deposit_id,
                    {
                        "principal_minor": 0,
                        "accrued_minor": 0,
                        "status": DepositStatus.WITHDRAWN.value,
                        "version": deposit.version + 1,
                    },
                )
                self.repo.update_holding(
                    holding["holding_id"],
                    {"settled_minor": payout, "status": "settled"},
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        profit = payout - principal
        self.bank.record_flow("wealth_settle", payout, detail=holding["holding_id"])
        return Result.success({
            "paid_minor": payout,
            "principal_minor": principal,
            "profit_minor": profit,
            "return_bps": return_bps,
        })

    def settle_matured(self, *, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """到期自动结算。由定时任务调用。"""
        current = int(now if now is not None else now_ms())
        rows = self.db.query_all(
            "SELECT * FROM bank_wealth_holdings WHERE status = 'active' "
            "AND matures_at_ms > 0 AND matures_at_ms <= ?",
            (current,),
        )
        out: List[Dict[str, Any]] = []
        for holding in rows:
            self.deposits.accrue_all(now=current)  # 先结清存款利息
            result = self.settle(holding, now=current)
            if result.ok:
                out.append({
                    "holder_account_id": holding["holder_account_id"],
                    **result.value,
                })
            elif self.log:
                self.log.error(
                    f"理财结算失败 {holding['holding_id']}: {result.code.value} {result.message}"
                )
        if out and self.log:
            self.log.info(f"已结算 {len(out)} 笔到期理财")
        return out

    def holdings_of(self, account_id: str) -> List[Dict[str, Any]]:
        return self.repo.holdings_of(account_id)

    def describe_product(self, product: Dict[str, Any]) -> str:
        risk = {1: "低", 2: "中", 3: "高"}.get(int(product.get("risk_level") or 1), "低")
        return (
            f"{product.get('name')} ｜ 期限 {product.get('term_days')} 天 ｜ "
            f"起投 {format_amount(int(product.get('min_amount_minor') or 0))} ｜ "
            f"预期年化 {int(product.get('expected_bps') or 0) / 100:.2f}% ｜ 风险{risk}"
        )
