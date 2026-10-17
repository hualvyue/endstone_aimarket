"""公司业务：注册、资产估值、纳税、破产清算。

**有限责任在这里是怎么落到实处的**
------------------------------------------------------------------
1. `accounts` 表有约束 `CHECK (balance_minor >= 0 OR kind = 'system')`，
   公司账户 kind = 'company'，所以它在数据库层面**永远不可能变成负数**；
2. 清算与追债的所有代码路径只接收 `borrower_account_id`（即公司账户），
   全程不会出现股东的个人 account_id；
3. 因此当公司资不抵债时，缺口只能在系统账户上核销，**结构上无法穿透到股东个人**。

这不是"我们记得不要动股东的钱"，而是"想动也动不了"。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import (
    AccountKind,
    AccountStatus,
    CompanyStatus,
    ShareStatus,
    TxKind,
)
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7, normalize_name
from ..core.money import apply_bps, format_amount
from ..core.result import Result
from ..economy.models import Entry, ROLE_PAYEE, ROLE_PAYER
from ..economy.repository import TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from .models import AssetBreakdown, Company, LiquidationPlan, Member
from .repository import CompanyRepository, now_ms

SEIZE_ACCOUNT_ID = "sys:mint"


class CompanyService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        pricing=None,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.pricing = pricing
        self.config = config
        self.log = log
        self.repo = CompanyRepository(db)

    # ==================== 配置 ====================

    def min_capital_minor(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("company.min_capital_minor", 100000))

    def default_turnover_tax_bps(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("company.turnover_tax_bps", 800))

    def default_dividend_bps(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("company.dividend_bps", 3000))

    def default_reserve_minor(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("company.reserve_minor", 0))

    def liquidation_fee_bps(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("company.liquidation_fee_bps", 0))

    def max_per_player(self) -> int:
        if self.config is None:
            return 1
        return int(self.config.as_int("company.max_per_player", 1))

    # ==================== 注册 ====================

    def register(
        self,
        *,
        founder_account_id: str,
        name: str,
        capital_minor: int,
        player=None,
    ) -> Result[Company]:
        """成立公司：发起人实缴注册资本，公司获得独立账户。

        注册资本是**实缴**的 —— 发起人把钱真的转进公司账户换取股份，
        不是记账上写一个数字。这样 NAV 才有真实含义。
        """
        clean = (name or "").strip()
        if len(clean) < 2 or len(clean) > 24:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "公司名长度需在 2~24 个字符之间")
        # 禁止私人开银行：存款与放贷是央行专营业务，公司根本没有这两个入口，
        # 这里连名字都拦掉，避免给出错误的预期。
        blocked = ("银行", "央行", "钱庄", "银号", "bank", "central bank")
        lowered = clean.lower()
        if any(word in lowered for word in blocked):
            return Result.failure(
                ErrorCode.SHOP_FORBIDDEN,
                "银行是央行专营业务，个人不得开设。请换个名字",
            )
        if self.repo.name_taken(clean):
            return Result.failure(ErrorCode.ACCOUNT_EXISTS, "这个公司名已被占用")

        capital = int(capital_minor)
        if capital < self.min_capital_minor():
            return Result.failure(
                ErrorCode.INVALID_AMOUNT,
                f"注册资本不足，至少需要 {format_amount(self.min_capital_minor())}",
            )

        founder = self.economy.repo.get_account(founder_account_id)
        if founder is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if founder.available_minor < capital:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，需要 {format_amount(capital)}，"
                f"当前可用 {format_amount(founder.available_minor)}",
            )

        limit = self.max_per_player()
        if limit > 0:
            # 只统计**在营**的公司：已破产/注销的不该继续占名额，
            # 否则玩家破产一次就永远不能再创业了。
            owned = [
                c for c in self.repo.list_by_member(founder_account_id)
                if c.founder_account_id == founder_account_id and c.is_active
            ]
            if len(owned) >= limit:
                return Result.failure(
                    ErrorCode.ACCOUNT_EXISTS,
                    f"每人最多只能同时经营 {limit} 家公司"
                    f"（你当前有 {len(owned)} 家，可以先注销或让它清算）",
                )

        account_id = "co:" + new_uuid7()
        try:
            with self.db.transaction():
                # 公司账户：kind='company' → 数据库层面禁负 → 有限责任的结构保证
                self.economy.repo.create_account(
                    account_id, AccountKind.COMPANY.value, name=clean
                )
                company = self.repo.create(
                    account_id=account_id,
                    name=clean,
                    founder_account_id=founder_account_id,
                    registered_capital_minor=capital,
                    turnover_tax_bps=self.default_turnover_tax_bps(),
                    dividend_bps=self.default_dividend_bps(),
                    reserve_minor=self.default_reserve_minor(),
                )
                if company is None:
                    raise AimarketError(ErrorCode.STORAGE_ERROR, "创建公司失败")

                if capital > 0:
                    moved = self.economy.post_in_transaction(
                        kind=TxKind.COMPANY_CAPITAL.value,
                        entries=[
                            Entry(founder_account_id, ROLE_PAYER, -capital),
                            Entry(account_id, ROLE_PAYEE, capital),
                        ],
                        idem=f"company-capital:{company.business_id}",
                        tx_id=f"co-capital:{company.business_id}",
                        actor=founder_account_id,
                        correlation_id=company.business_id,
                        gross=capital,
                        net=capital,
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        except Exception as e:
            if self.log:
                self.log.error(f"注册公司异常: {e}")
            return Result.failure(ErrorCode.INTERNAL, str(e))

        if self.log:
            self.log.info(f"公司 {clean} 成立，注册资本 {format_amount(capital)}")
        return Result.success(self.repo.get(company.business_id))

    # ==================== 权限 ====================

    def can_operate(self, company: Company, account_id: str) -> bool:
        """只有法人代表（发起人）能代表公司操作，避免"谁都能动公司的钱"。"""
        return self.repo.is_founder(company.business_id, account_id)

    def is_member(self, company: Company, account_id: str) -> bool:
        return self.repo.get_member(company.business_id, account_id) is not None

    def members(self, company: Company) -> List[Member]:
        return self.repo.members(company.business_id)

    # ==================== 资产估值 ====================

    def assets(self, company: Company) -> AssetBreakdown:
        """算出公司真实资产。股价和破产清算都用这一份口径，保证两处一致。"""
        breakdown = AssetBreakdown()
        breakdown.cash_minor = self.economy.get_balance(company.account_id).balance_minor

        shops = self.repo.shops_of(company.account_id)
        breakdown.shop_count = len(shops)
        for shop in shops:
            escrow = shop.get("escrow_account_id")
            if escrow:
                breakdown.escrow_minor += self.economy.get_balance(escrow).balance_minor

            item = _decode(shop.get("offer_item_json"))
            stock = int(shop.get("stock_qty", 0) or 0) - int(shop.get("held_stock_qty", 0) or 0)
            if stock > 0 and item.get("type"):
                value = self._unit_value(item["type"])
                if value > 0:
                    breakdown.stock_value_minor += stock * value
                else:
                    breakdown.unpriced_items += 1

            for record in _decode_list(shop.get("collected_json")):
                count = int(record.get("count", 0) or 0)
                item_type = record.get("type")
                if count <= 0 or not item_type:
                    continue
                value = self._unit_value(item_type)
                if value > 0:
                    breakdown.collected_value_minor += count * value
                else:
                    breakdown.unpriced_items += 1

        breakdown.debt_minor = self.repo.outstanding_debt(company.account_id)
        return breakdown

    def nav_minor(self, company: Company) -> int:
        return self.assets(company).nav_minor

    def _unit_value(self, item_type: str) -> int:
        """物品估值：一律用官方**回收价**（比标价保守，防止估值虚高被用来操纵股价）。
        没有官方定价的物品按 0 计，不虚增。"""
        if self.pricing is None:
            return 0
        try:
            return int(self.pricing.quote_price(item_type, "sell") or 0)
        except Exception:
            return 0

    # ==================== 税务 ====================

    def tax_bps_for_owner(self, owner_account_id: str) -> int:
        """按店主的主体类型给税率：个人走个人税率，公司走公司营业额税率。"""
        account = self.economy.repo.get_account(owner_account_id)
        if account is not None and account.kind == AccountKind.COMPANY.value:
            company = self.repo.get_by_account(owner_account_id)
            if company is not None and company.turnover_tax_bps > 0:
                return int(company.turnover_tax_bps)
            return self.default_turnover_tax_bps()
        if self.config is None:
            return 0
        return int(self.config.as_int("economy.tax_bps", 0))

    def record_turnover(
        self, owner_account_id: str, *, gross_minor: int, tax_minor: int,
        shop_id: Optional[str] = None, direction: Optional[str] = None,
    ) -> None:
        """记录一笔公司营业额（供统计与分红决策）。真正的扣税在交易事务内完成。"""
        company = self.repo.get_by_account(owner_account_id)
        if company is None:
            return
        self.repo.record_turnover(
            business_id=company.business_id,
            gross_minor=gross_minor,
            tax_minor=tax_minor,
            shop_id=shop_id,
            direction=direction,
        )

    # ==================== 破产清算 ====================

    def plan_liquidation(self, company: Company) -> LiquidationPlan:
        """先算清楚清偿顺序与金额，再执行 —— 避免边算边改导致账目半途错乱。"""
        breakdown = self.assets(company)
        plan = LiquidationPlan(
            company_id=company.business_id,
            gross_minor=breakdown.gross_minor,
            debt_minor=breakdown.debt_minor,
        )

        remaining = breakdown.gross_minor
        # 顺位 1：清算费用
        fee = apply_bps(breakdown.gross_minor, self.liquidation_fee_bps())
        if fee > 0:
            paid = min(fee, remaining)
            plan.payouts.append(("清算费用", fee, paid))
            remaining -= paid

        # 顺位 2：制度性应缴（税款在成交时已实时扣清，这里通常为 0，保留顺位便于以后扩展）
        plan.payouts.append(("应缴税款", 0, 0))

        # 顺位 3：债权。按「有担保 → 玩家出资 → 银行」依次清偿
        debts = self._debt_rows(company.account_id)
        tiers = (
            ("有担保债权", [d for d in debts if d.get("guarantee_account_id")]),
            ("投资者债权（融资）", [d for d in debts
                                   if not d.get("guarantee_account_id")
                                   and d.get("lender_kind") == "player"]),
            ("银行债权", [d for d in debts
                          if not d.get("guarantee_account_id")
                          and d.get("lender_kind") != "player"]),
        )
        for label, rows in tiers:
            due = sum(int(d["outstanding"]) for d in rows)
            if due <= 0:
                continue
            paid = min(due, remaining)
            plan.payouts.append((label, due, paid))
            remaining -= paid

        # 顺位 4：股东（最后）
        plan.to_shareholders_minor = max(0, remaining)
        plan.writeoff_minor = max(0, breakdown.debt_minor - plan.total_paid - fee)
        return plan

    def liquidate(self, company: Company, *, reason: str = "insolvent") -> Result[LiquidationPlan]:
        """执行破产清算。

        资金只来自「公司账户 + 名下店铺库存变卖 + 托管余额」，
        分录里从头到尾不出现股东的个人账户 —— 缺口由系统账户核销。
        这就是"公司破产不牵连股东"的实现方式。
        """
        if company.status not in (CompanyStatus.ACTIVE.value, CompanyStatus.LIQUIDATING.value):
            return Result.failure(ErrorCode.TX_STATE, "该公司当前状态无法清算")

        plan = self.plan_liquidation(company)

        try:
            with self.db.transaction():
                # ① 冻结：先把公司和名下店铺锁住，杜绝清算过程中还有新交易进来
                self.repo.set_status(company.business_id, CompanyStatus.LIQUIDATING.value)
                self.db.update(
                    "shops", {"status": "closed"}, "owner_account_id = ?", (company.account_id,)
                )
                self._suspend_shares(company)

                # ② 变现：库存与收货按官方回收价折成现金进公司账户；托管余额转回
                proceeds = self._realize_assets(company)

                # ③ 清偿
                account_id = company.account_id
                available = self.economy.get_balance(account_id).balance_minor

                debts = self._debt_rows(account_id)
                paid_total = 0
                for row in debts:
                    loan_id = row["loan_id"]
                    due = int(row["outstanding"])
                    if due <= 0:
                        continue
                    pay = min(due, max(0, available))
                    if pay <= 0:
                        # 资不抵债：剩余部分核销，体现为已投放货币增加（不会落到股东头上）
                        self.db.update(
                            "loans",
                            {
                                "status": "written_off",
                                "outstanding_principal_minor": 0,
                                "accrued_interest_minor": 0,
                                "updated_at_ms": now_ms(),
                            },
                            "loan_id = ?",
                            (loan_id,),
                        )
                        continue
                    # 钱要付给真正的债权人：玩家出资的融资就付给他本人，
                    # 银行债权才回流到系统账户
                    lender_kind = row.get("lender_kind") or "bank"
                    creditor = row.get("lender_account_id") if lender_kind == "player" else None
                    creditor = creditor or SEIZE_ACCOUNT_ID
                    repaid = self.economy.post_in_transaction(
                        kind=TxKind.LOAN_SEIZE.value,
                        entries=[
                            Entry(account_id, ROLE_PAYER, -pay),
                            Entry(creditor, ROLE_PAYEE, pay),
                        ],
                        idem=f"liquidate-repay:{loan_id}",
                        tx_id=f"liq-repay:{loan_id}",
                        actor=account_id,
                        correlation_id=company.business_id,
                        gross=pay,
                        net=pay,
                    )
                    if not repaid.ok:
                        raise AimarketError(repaid.code, repaid.message)
                    available -= pay
                    paid_total += pay
                    leftover = max(0, due - pay)
                    self.db.update(
                        "loans",
                        {
                            "status": "settled" if leftover == 0 else "written_off",
                            "outstanding_principal_minor": leftover,
                            "accrued_interest_minor": 0,
                            "updated_at_ms": now_ms(),
                        },
                        "loan_id = ?",
                        (loan_id,),
                    )

                # ④ 剩余按持股分给股东（有限责任下通常为 0）
                remaining = self.economy.get_balance(account_id).balance_minor
                if remaining > 0:
                    self._distribute_to_shareholders(company, remaining)

                # ⑤ 收尾：公司账户关闭，股份退市
                self.db.update(
                    "accounts",
                    {"status": AccountStatus.CLOSED.value, "updated_at_ms": now_ms()},
                    "account_id = ?",
                    (account_id,),
                )
                self.repo.set_status(company.business_id, CompanyStatus.BANKRUPT.value)
                self._delist_shares(company)
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        except Exception as e:
            if self.log:
                import traceback
                self.log.error(f"清算 {company.name} 异常: {e}\n{traceback.format_exc()}")
            return Result.failure(ErrorCode.INTERNAL, str(e))

        plan.notes.append("变现所得 %d，已清偿 %d，核销 %d" % (
            plan.gross_minor, plan.total_paid, plan.writeoff_minor
        ))
        if self.log:
            self.log.warning(f"公司 {company.name} 破产清算完成（{reason}）：{plan.describe()}")
        return Result.success(plan)

    # ---------- 清算内部步骤 ----------

    def _realize_assets(self, company: Company) -> int:
        """把公司名下的库存与托管余额折成现金，放进公司账户。返回变现总额。"""
        account_id = company.account_id
        total = 0

        for shop in self.repo.shops_of(account_id):
            # 出售店库存 → 按回收价卖给系统（物品本就是虚拟登记，这里直接冲销数量）
            item = _decode(shop.get("offer_item_json"))
            stock = int(shop.get("stock_qty", 0) or 0)
            if stock > 0 and item.get("type"):
                unit = self._unit_value(item["type"])
                if unit > 0:
                    total += stock * unit
            # 收购店的收货同理
            for record in _decode_list(shop.get("collected_json")):
                count = int(record.get("count", 0) or 0)
                unit = self._unit_value(record.get("type"))
                if count > 0 and unit > 0:
                    total += count * unit

            self.db.update(
                "shops",
                {"stock_qty": 0, "held_stock_qty": 0, "collected_json": None, "updated_at_ms": now_ms()},
                "shop_id = ?",
                (shop["shop_id"],),
            )

            # 托管余额转回公司账户
            escrow = shop.get("escrow_account_id")
            if escrow:
                escrow_balance = self.economy.get_balance(escrow).balance_minor
                if escrow_balance > 0:
                    moved = self.economy.post_in_transaction(
                        kind=TxKind.SHOP_WITHDRAW.value,
                        entries=[
                            Entry(escrow, ROLE_PAYER, -escrow_balance),
                            Entry(account_id, ROLE_PAYEE, escrow_balance),
                        ],
                        idem=f"liquidate-escrow:{shop['shop_id']}",
                        tx_id=f"liq-escrow:{shop['shop_id']}",
                        actor=account_id,
                        correlation_id=shop["shop_id"],
                        gross=escrow_balance,
                        net=escrow_balance,
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)

        if total > 0:
            # 变卖所得 = 系统回收了这些物品并付钱给公司（等价于一次铸币）
            realized = self.economy.post_in_transaction(
                kind=TxKind.MINT.value,
                entries=[
                    Entry(SEIZE_ACCOUNT_ID, "source", -total),
                    Entry(account_id, ROLE_PAYEE, total),
                ],
                idem=f"liquidate-proceeds:{company.business_id}",
                tx_id=f"liq-proceeds:{company.business_id}",
                actor=account_id,
                correlation_id=company.business_id,
                gross=total,
                net=total,
            )
            if not realized.ok:
                raise AimarketError(realized.code, realized.message)
        return total

    def _distribute_to_shareholders(self, company: Company, amount: int) -> None:
        """把清算剩余分给出资人。

        规则：
          * 已经发过股票 → 按持股比例分配；
          * **没有股份记录 → 全部给发起人**（未上市公司的股权 100% 属于他）。
            这一条很关键：漏掉的话剩余资产会困在一个已关闭的公司账户里，
            既取不出来也进不了任何人的口袋，等于凭空消失。
        分配过程只动公司账户，不涉及股东既有财产。
        """
        holdings = self.db.query_all(
            "SELECT holder_account_id, shares FROM share_holdings "
            "WHERE company_account_id = ? AND shares > 0 ORDER BY shares DESC",
            (company.account_id,),
        )
        total_shares = sum(int(h["shares"]) for h in holdings)

        if total_shares <= 0:
            # 未发行股份：发起人是唯一出资人
            target = company.founder_account_id
            result = self.economy.post_in_transaction(
                kind=TxKind.COMPANY_LIQUIDATE.value,
                entries=[
                    Entry(company.account_id, ROLE_PAYER, -amount),
                    Entry(target, ROLE_PAYEE, amount),
                ],
                idem=f"liquidate-dist:{company.business_id}",
                tx_id=f"liq-dist:{company.business_id}",
                actor=company.account_id,
                correlation_id=company.business_id,
                gross=amount,
                net=amount,
            )
            if not result.ok:
                raise AimarketError(result.code, result.message)
            return

        entries = [Entry(company.account_id, ROLE_PAYER, -amount)]
        distributed = 0
        for index, holding in enumerate(holdings):
            shares = int(holding["shares"])
            cut = amount * shares // total_shares
            if index == len(holdings) - 1:
                cut = amount - distributed  # 尾差给最后一位，保证分文不差
            if cut <= 0:
                continue
            entries.append(Entry(holding["holder_account_id"], f"sh{index}", cut))
            distributed += cut

        if distributed <= 0:
            return
        result = self.economy.post_in_transaction(
            kind=TxKind.COMPANY_LIQUIDATE.value,
            entries=entries,
            idem=f"liquidate-dist:{company.business_id}",
            tx_id=f"liq-dist:{company.business_id}",
            actor=company.account_id,
            correlation_id=company.business_id,
            gross=amount,
            net=amount,
        )
        if not result.ok:
            raise AimarketError(result.code, result.message)

    def _debt_rows(self, account_id: str) -> List[Dict[str, Any]]:
        """取债务清单，**按清偿顺位排好序**：

            0. 有担保债权
            1. 玩家出资的债权（融资）—— 排在银行之前，优先保护散户
            2. 银行债权 —— 它是增发出来的，承受坏账的能力最强

        这样"玩家借给公司的钱"不会在清算时被银行挤到后面去。
        """
        rows = self.db.query_all(
            """SELECT loan_id, lender_account_id, lender_kind,
                      outstanding_principal_minor + accrued_interest_minor AS outstanding,
                      guarantee_account_id, is_company
                 FROM loans
                WHERE borrower_account_id = ? AND status IN ('active','overdue','collecting')
                ORDER BY
                    CASE WHEN guarantee_account_id IS NOT NULL THEN 0
                         WHEN lender_kind = 'player' THEN 1
                         ELSE 2 END,
                    due_at_ms""",
            (account_id,),
        )
        return [dict(r) for r in rows]

    def _suspend_shares(self, company: Company) -> None:
        self.db.update(
            "company_shares",
            {"status": ShareStatus.SUSPENDED.value, "updated_at_ms": now_ms()},
            "company_account_id = ?",
            (company.account_id,),
        )

    def _delist_shares(self, company: Company) -> None:
        self.db.update(
            "company_shares",
            {
                "status": ShareStatus.DELISTED.value,
                "last_price_minor": 0,
                "nav_minor": 0,
                "nav_per_share_minor": 0,
                "updated_at_ms": now_ms(),
            },
            "company_account_id = ?",
            (company.account_id,),
        )
        # 持股记录保留供审计，但股东个人账户分文未动

    # ==================== 查询 ====================

    def get(self, business_id: str) -> Optional[Company]:
        return self.repo.get(business_id)

    def find(self, key: str) -> Optional[Company]:
        company = self.repo.get(key)
        if company is not None:
            return company
        by_name = self.repo.get_by_name(key)
        if by_name is not None:
            return by_name
        return self.repo.get_by_account(key)

    def list_all(self, *, active_only: bool = True) -> List[Company]:
        return self.repo.list_all(active_only=active_only)

    def companies_of(self, account_id: str) -> List[Company]:
        return self.repo.list_by_member(account_id)

    def summary(self, company: Company) -> Dict[str, Any]:
        breakdown = self.assets(company)
        return {
            "company": company,
            "assets": breakdown,
            "nav_minor": breakdown.nav_minor,
            "member_count": self.repo.member_count(company.business_id),
            "shop_count": breakdown.shop_count,
        }


# ---------- 小工具 ----------

def _decode(payload) -> Dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_list(payload) -> List[Dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return payload
    try:
        data = json.loads(payload)
        return data if isinstance(data, list) else []
    except Exception:
        return []
