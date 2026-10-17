"""公司募资：投资 / 融资 / 赞助。

三者都是「公司拿到钱」，差别只在出资方换到了什么：

    投资 invest   → 股份。参与分红，承担股价涨跌，股权被稀释。
    融资 finance  → 债权。到期还本付息，不占股；清算时排在银行之前受偿。
    赞助 sponsor  → 广告位。公司名下实体店的详情页会署名"本店由 XXX 赞助"。

为什么单独做一个编排层，而不是塞进 company 或 stock
------------------------------------------------------------------
这三种方式分别要用到公司、贷款、股票三个服务。如果把它们写进 company，
company 就要反向依赖 loan 和 stock，而那两个本来就依赖 company —— 直接成环。
放在这里做编排，依赖方向保持单向：fundraising → {company, loan, stock}。

四道防滥用闸门（全部可配置，设为 0 即关闭）
------------------------------------------------------------------
    per_funder_limit_minor  同一玩家对同一公司的累计出资上限
    cooldown_hours          两次募资之间的间隔
    nav_multiplier_bps      累计募资总额 ≤ 公司净资产 × 倍数
    lock_days               募资到账后 N 天内禁止给发起人分红（防"募完就跑"）
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import CompanyStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7
from ..core.money import format_amount
from ..core.result import Result
from ..economy.models import Entry, ROLE_PAYEE, ROLE_PAYER
from ..economy.service import EconomyService
from .models import FundraisingRecord, Sponsorship

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS

KIND_INVEST = "invest"
KIND_FINANCE = "finance"
KIND_SPONSOR = "sponsor"


def now_ms() -> int:
    return int(time.time() * 1000)


class FundraisingService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        company_service,
        loan_service,
        stock_service,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.companies = company_service
        self.loans = loan_service
        self.stocks = stock_service
        self.config = config
        self.log = log

    # ==================== 配置 ====================

    def _cfg_int(self, path: str, default: int) -> int:
        if self.config is None:
            return default
        try:
            return int(self.config.as_int(path, default))
        except Exception:
            return default

    def per_funder_limit_minor(self) -> int:
        return self._cfg_int("fundraising.per_funder_limit_minor", 0)

    def cooldown_hours(self) -> int:
        return self._cfg_int("fundraising.cooldown_hours", 24)

    def nav_multiplier_bps(self) -> int:
        return self._cfg_int("fundraising.nav_multiplier_bps", 10000)

    def lock_days(self) -> int:
        return self._cfg_int("fundraising.lock_days", 3)

    def min_amount_minor(self) -> int:
        return self._cfg_int("fundraising.min_amount_minor", 10000)

    def sponsor_min_days(self) -> int:
        return max(1, self._cfg_int("fundraising.sponsor_min_days", 1))

    # ==================== 防滥用闸门 ====================

    def _fresh(self, company):
        """重新读一次公司，避免拿调用方手里那份过期的累计募资额做判断。"""
        latest = self.companies.repo.get(company.business_id) if self.companies else None
        return latest or company

    def _guard(
        self, company, funder_account_id: str, amount_minor: int, *, kind: str
    ) -> Result[None]:
        """四种出资共用的前置校验。任何一条不满足就拒绝。"""
        company = self._fresh(company)
        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if amount < self.min_amount_minor():
            return Result.failure(
                ErrorCode.INVALID_AMOUNT,
                f"单次出资不能低于 {format_amount(self.min_amount_minor())}",
            )
        if company.status != CompanyStatus.ACTIVE.value:
            return Result.failure(ErrorCode.TX_STATE, "该公司当前不接受募资")
        if funder_account_id == company.account_id:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "公司不能给自己出资")

        funder = self.economy.repo.get_account(funder_account_id)
        if funder is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND, "出资方账户不存在")
        if funder.available_minor < amount:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，需要 {format_amount(amount)}，"
                f"当前可用 {format_amount(funder.available_minor)}",
            )

        # —— 闸门 1：单人上限（投资+融资+赞助合计）——
        limit = self.per_funder_limit_minor()
        if limit > 0:
            already = self.total_from_funder(company.account_id, funder_account_id)
            if already + amount > limit:
                return Result.failure(
                    ErrorCode.SUPPLY_LIMIT,
                    f"你对这家公司的累计出资已达上限（{format_amount(limit)}）——"
                    f"已出资 {format_amount(already)}",
                )

        # —— 闸门 2：募资冷却（只对「投资」生效）——
        #
        # 为什么只卡投资：投资会直接推高公司现金从而推高 NAV，而额度又挂在 NAV 上，
        # 于是形成「投资 → NAV 上升 → 额度上升 → 再投资」的循环。融资则不同 ——
        # 现金与负债同步增加，NAV 分毫不变，没有这个反馈回路。
        # 如果对所有方式都冷却，正常的一次融资+一次赞助会被硬拖一天，很难用。
        cooldown = self.cooldown_hours()
        if kind == KIND_INVEST and cooldown > 0:
            last = self._last_invest_ms(company.account_id)
            if last > 0 and now_ms() - last < cooldown * HOUR_MS:
                remain = cooldown * HOUR_MS - (now_ms() - last)
                return Result.failure(
                    ErrorCode.TX_BUSY,
                    f"这家公司刚刚接受过投资，请等待约 {remain // HOUR_MS + 1} 小时后再投",
                )

        # —— 闸门 3：总量与公司自有资产挂钩 ——
        #
        # 关键在「自有资产 = NAV − 已募资金」这一步。若直接用 NAV 当基数，
        # 公司募到的钱会立刻抬高自己的基数，额度随之水涨船高，
        # 变成 1000 → 可募 1000 → NAV 2000 → 可募 2000 → …… 的指数膨胀。
        # 扣掉已募部分后，上限锁死在最初的自由资产上，募资无法自抬门槛。
        multiplier = self.nav_multiplier_bps()
        if multiplier > 0:
            nav = self.companies.nav_minor(company)
            self_funded = max(0, nav - company.total_raised_minor)
            if self_funded <= 0:
                return Result.failure(
                    ErrorCode.SHOP_NO_BUDGET,
                    "公司当前没有自有净资产可供支撑募资（资不抵债的公司不能募）",
                )
            ceiling = self_funded * multiplier // 10000
            if company.total_raised_minor + amount > ceiling:
                return Result.failure(
                    ErrorCode.SUPPLY_LIMIT,
                    f"累计募资已达公司自有资产的 {multiplier / 100:.0f}% 上限 —— "
                    f"已募 {format_amount(company.total_raised_minor)}，上限 {format_amount(ceiling)}",
                )
        return Result.success(None)

    def _last_invest_ms(self, company_account_id: str) -> int:
        row = self.db.query_one(
            "SELECT MAX(created_at_ms) AS t FROM company_fundraising "
            "WHERE company_account_id = ? AND kind = ?",
            (company_account_id, KIND_INVEST),
        )
        return int((row or {}).get("t") or 0)

    def _after_fundraise(self, company, amount_minor: int, funder_account_id: str,
                         kind: str, units: int = 0, ledger_tx_id: str = "") -> None:
        """募资成功后的统一收尾：更新累计额、冷却起点、锁定到期时间。"""
        company = self._fresh(company)
        lock_days = self.lock_days()
        locked_until = company.fundraising_locked_until_ms
        if lock_days > 0:
            locked_until = max(locked_until, now_ms() + lock_days * DAY_MS)
        self.companies.repo.update(
            company.business_id,
            {
                "total_raised_minor": company.total_raised_minor + amount_minor,
                "last_fundraise_ms": now_ms(),
                "fundraising_locked_until_ms": locked_until,
            },
        )
        self.db.insert(
            "company_fundraising",
            {
                "record_id": new_uuid7(),
                "company_account_id": company.account_id,
                "funder_account_id": funder_account_id,
                "kind": kind,
                "amount_minor": int(amount_minor),
                "units": int(units),
                "ledger_tx_id": ledger_tx_id or None,
                "created_at_ms": now_ms(),
            },
        )

    # ==================== 投资 ====================

    def invest(self, *, company, funder_account_id: str, amount_minor: int) -> Result[Dict[str, Any]]:
        """投资：按当前股价把出资额换成股份。

        与直接 `/stock buy` 的区别是这里按**金额**下单，更适合"我想投你 500 金"
        这种表达；股数由金额和当时的股价决定。
        """
        guard = self._guard(company, funder_account_id, amount_minor, kind=KIND_INVEST)
        if not guard.ok:
            return guard

        listing = self.stocks.listing_of(company.account_id) if self.stocks else None
        if listing is None:
            return Result.failure(
                ErrorCode.SHOP_NOT_FOUND, "该公司尚未上市，无法接受投资（可以先融资或赞助）"
            )
        price = int(listing.last_price_minor)
        if price <= 0:
            return Result.failure(ErrorCode.PRICE_NOT_SET, "当前没有有效股价")

        shares = int(amount_minor) // price
        if shares <= 0:
            return Result.failure(
                ErrorCode.INVALID_AMOUNT,
                f"出资金额不足一股（当前股价 {format_amount(price)}）",
            )
        actual_amount = shares * price

        issued = self.stocks.issue(
            company_account_id=company.account_id,
            buyer_account_id=funder_account_id,
            shares=shares,
        )
        if not issued.ok:
            return Result.failure(issued.code, issued.message)

        self._after_fundraise(
            company, actual_amount, funder_account_id, KIND_INVEST, units=shares
        )
        order = issued.value
        return Result.success(
            {
                "kind": KIND_INVEST,
                "shares": shares,
                "unit_price_minor": price,
                "amount_minor": actual_amount,
                "holding": order.new_holding,
                "note": f"实付 {format_amount(actual_amount)}（不足一股的余数未扣）",
            }
        )

    # ==================== 融资 ====================

    def finance(
        self, *, company, funder_account_id: str, amount_minor: int, term_days: Optional[int] = None
    ) -> Result[Dict[str, Any]]:
        """融资：玩家把钱借给公司，成为公司的债权人，到期按约定利率收回本息。

        复用贷款系统：债务的计息、逾期、追偿、清算受偿顺序全都现成，
        只是债权人是玩家而不是系统银行。
        """
        guard = self._guard(company, funder_account_id, amount_minor, kind=KIND_FINANCE)
        if not guard.ok:
            return guard

        term = int(term_days or company.finance_term_days or 30)
        rate = int(company.finance_rate_bps_per_day or 0)

        result = self.loans.borrow(
            borrower_account_id=company.account_id,
            amount_minor=int(amount_minor),
            term_days=term,
            rate_bps_per_day=rate,
            lender_account_id=funder_account_id,
            lender_kind="player",
            reason=f"company_finance:{company.business_id}",
        )
        if not result.ok:
            return Result.failure(result.code, result.message)

        loan = result.value
        self._after_fundraise(
            company, int(amount_minor), funder_account_id, KIND_FINANCE, units=term
        )
        return Result.success(
            {
                "kind": KIND_FINANCE,
                "amount_minor": int(amount_minor),
                "rate_bps_per_day": rate,
                "term_days": term,
                "due_at_ms": loan.due_at_ms,
                "expected_interest_minor": (
                    int(amount_minor) * rate * term * 24 // (10000 * 24)
                ),
                "note": f"日息 {rate / 100:.2f}%，{term} 天后到期还本付息",
            }
        )

    # ==================== 赞助 ====================

    def sponsor(self, *, company, sponsor_account_id: str, amount_minor: int, days: int) -> Result[Dict[str, Any]]:
        """赞助：出资换取广告位。公司名下实体店的详情页会署名赞助商。

        纯资金支持 + 一次署名，不占股也不产生债务。
        """
        guard = self._guard(company, sponsor_account_id, amount_minor, kind=KIND_SPONSOR)
        if not guard.ok:
            return guard

        period = max(self.sponsor_min_days(), int(days or 1))
        amount = int(amount_minor)
        sponsor = self.economy.repo.get_account(sponsor_account_id)
        display_name = (sponsor.current_name if sponsor else None) or "匿名赞助商"

        try:
            with self.db.transaction():
                receipt = self.economy.post_in_transaction(
                    kind=TxKind.TRANSFER.value,
                    entries=[
                        Entry(sponsor_account_id, ROLE_PAYER, -amount),
                        Entry(company.account_id, ROLE_PAYEE, amount),
                    ],
                    idem=f"sponsor:{company.business_id}:{new_uuid7()}",
                    tx_id=f"sponsor:{new_uuid7()}",
                    actor=sponsor_account_id,
                    correlation_id=company.business_id,
                    metadata={"kind": KIND_SPONSOR, "days": period},
                    gross=amount,
                    net=amount,
                )
                if not receipt.ok:
                    raise AimarketError(receipt.code, receipt.message)

                self.db.insert(
                    "company_sponsorships",
                    {
                        "sponsorship_id": new_uuid7(),
                        "company_account_id": company.account_id,
                        "sponsor_account_id": sponsor_account_id,
                        "amount_minor": amount,
                        "display_name": display_name,
                        "starts_at_ms": now_ms(),
                        "expires_at_ms": now_ms() + period * DAY_MS,
                        "status": "active",
                        "created_at_ms": now_ms(),
                    },
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        self._after_fundraise(company, amount, sponsor_account_id, KIND_SPONSOR, units=period)
        return Result.success(
            {
                "kind": KIND_SPONSOR,
                "amount_minor": amount,
                "days": period,
                "display_name": display_name,
                "expires_at_ms": now_ms() + period * DAY_MS,
                "note": f"赞助期 {period} 天，公司名下店铺详情页将署名「{display_name}」",
            }
        )

    # ==================== 统一入口 ====================

    def contribute(
        self, *, company, funder_account_id: str, kind: str, amount_minor: int, days: int = 1
    ) -> Result[Dict[str, Any]]:
        """按类型分发，方便命令与 GUI 统一调用。"""
        normalized = (kind or "").strip().lower()
        if normalized in ("invest", "投资", "投资入股"):
            return self.invest(
                company=company, funder_account_id=funder_account_id, amount_minor=amount_minor
            )
        if normalized in ("finance", "融资"):
            return self.finance(
                company=company, funder_account_id=funder_account_id,
                amount_minor=amount_minor, term_days=days if days > 1 else None,
            )
        if normalized in ("sponsor", "赞助"):
            return self.sponsor(
                company=company, sponsor_account_id=funder_account_id,
                amount_minor=amount_minor, days=max(1, days),
            )
        return Result.failure(ErrorCode.INVALID_AMOUNT, "出资方式只能是 投资 / 融资 / 赞助")

    # ==================== 查询 ====================

    def total_from_funder(self, company_account_id: str, funder_account_id: str) -> int:
        row = self.db.query_one(
            """SELECT COALESCE(SUM(amount_minor), 0) AS total FROM company_fundraising
                WHERE company_account_id = ? AND funder_account_id = ?""",
            (company_account_id, funder_account_id),
        )
        return int((row or {}).get("total") or 0)

    def breakdown(self, company_account_id: str) -> Dict[str, int]:
        rows = self.db.query_all(
            """SELECT kind, COALESCE(SUM(amount_minor),0) AS total, COUNT(*) AS n
                 FROM company_fundraising WHERE company_account_id = ? GROUP BY kind""",
            (company_account_id,),
        )
        out = {KIND_INVEST: 0, KIND_FINANCE: 0, KIND_SPONSOR: 0}
        for row in rows:
            out[row["kind"]] = int(row["total"])
        return out

    def records_of_company(self, company_account_id: str, *, limit: int = 30) -> List[FundraisingRecord]:
        rows = self.db.query_all(
            """SELECT f.*, a.current_name AS funder_name FROM company_fundraising f
                 LEFT JOIN accounts a ON a.account_id = f.funder_account_id
                WHERE f.company_account_id = ? ORDER BY f.created_at_ms DESC LIMIT ?""",
            (company_account_id, int(limit)),
        )
        return [FundraisingRecord.from_row(r) for r in rows]

    def records_of_funder(self, funder_account_id: str, *, limit: int = 30) -> List[FundraisingRecord]:
        rows = self.db.query_all(
            """SELECT f.*, a.current_name AS funder_name FROM company_fundraising f
                 LEFT JOIN accounts a ON a.account_id = f.funder_account_id
                WHERE f.funder_account_id = ? ORDER BY f.created_at_ms DESC LIMIT ?""",
            (funder_account_id, int(limit)),
        )
        return [FundraisingRecord.from_row(r) for r in rows]

    # ---------- 赞助（广告）----------

    def active_sponsors(self, company_account_id: str) -> List[Sponsorship]:
        """公司当前在有效期内的赞助商。店铺详情页署名用这个。"""
        rows = self.db.query_all(
            """SELECT * FROM company_sponsorships
                WHERE company_account_id = ? AND status = 'active' AND expires_at_ms > ?
                ORDER BY amount_minor DESC""",
            (company_account_id, now_ms()),
        )
        return [Sponsorship.from_row(r) for r in rows]

    def sponsor_line(self, company_account_id: str) -> Optional[str]:
        """生成一行署名文本，没有赞助商时返回 None。"""
        sponsors = self.active_sponsors(company_account_id)
        if not sponsors:
            return None
        names = "、".join(s.display_name or "?" for s in sponsors[:3])
        return names

    def expire_sponsorships(self) -> int:
        """把过期的赞助置为 expired。由定时任务调用。"""
        return self.db.update(
            "company_sponsorships",
            {"status": "expired"},
            "status = 'active' AND expires_at_ms <= ?",
            (now_ms(),),
        )

    def sponsorship_summary(self, company_account_id: str) -> Dict[str, Any]:
        active = self.active_sponsors(company_account_id)
        row = self.db.query_one(
            """SELECT COALESCE(SUM(amount_minor),0) AS total, COUNT(*) AS n
                 FROM company_sponsorships WHERE company_account_id = ?""",
            (company_account_id,),
        ) or {}
        latest_expiry = max((s.expires_at_ms for s in active), default=0)
        return {
            "active_count": len(active),
            "total_minor": int(row.get("total") or 0),
            "all_count": int(row.get("n") or 0),
            "latest_expiry_ms": latest_expiry,
            "sponsors": active,
        }

    def is_locked(self, company) -> bool:
        """公司是否处于募资锁定期（期内禁止给发起人分红）。"""
        return company.fundraising_locked_until_ms > now_ms()

    def lock_remaining_hours(self, company) -> int:
        if not self.is_locked(company):
            return 0
        return max(0, (company.fundraising_locked_until_ms - now_ms()) // HOUR_MS) + 1
