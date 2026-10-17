"""股票业务：上市、申购、赎回、分红、定价。

**申购的原子性与稀释**
------------------------------------------------------------------
申购时，玩家付的钱进入公司账户，同时流通股增加。两者在**同一个事务**里完成：

    每股净资产 = (NAV + N×P) / (流通股 + N)

若申购价 P 恰好等于申购前的每股净资产 NAV/流通股，上式恒等于 NAV/流通股 ——
老股东的每股价值分毫不变。

但本功能是**系统做市**：报价 = 每股净资产 × (1 ± 随机指数)，所以 P 通常会偏离
每股净资产，从而带来同量级的稀释或增益（幅度受 `stock.rand_bps` 约束，默认 ±5%）。
这是做市模式的固有特性，不是 bug。若要求完全无稀释，就用 `nav_per_share` 定价发行，
代价是价格脱离"市场价"。

真正的生命线是那两步的**原子性**：如果拆到不同事务里提交（钱先进来、股票下次再发），
且发行价低于每股净资产，就等于拿老股东的钱补贴新股东。
"""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

from ..core.constants import AccountKind, ShareStatus, TxKind
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7
from ..core.money import apply_bps, format_amount
from ..core.result import Result
from ..economy.models import Entry, ROLE_PAYEE, ROLE_PAYER
from ..economy.repository import TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from .models import DividendResult, Holding, OrderResult, Quote, ShareListing
from .repository import StockRepository, now_ms
from .valuation import ValuationConfig, compute_price, nav_per_share

HOUR_MS = 3600 * 1000


class StockService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        company_service=None,
        *,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.companies = company_service
        self.config = config
        self.log = log
        self.repo = StockRepository(db)
        self._rng = random.Random()

    # ==================== 配置 ====================

    def valuation_config(self) -> ValuationConfig:
        cfg = self.config
        return ValuationConfig(
            rand_bps=cfg.as_int("stock.rand_bps", 500) if cfg else 500,
            limit_bps=cfg.as_int("stock.limit_bps", 2000) if cfg else 2000,
            min_price_minor=1,
        )

    def min_capital_minor(self) -> int:
        return self.config.as_int("stock.min_capital_minor", 100000) if self.config else 100000

    def default_tax_bps(self) -> int:
        return self.config.as_int("stock.dividend_tax_bps", 500) if self.config else 500

    def min_price_minor(self) -> int:
        return self.config.as_int("stock.min_issue_price_minor", 100) if self.config else 100

    # ==================== 上市 ====================

    def list_company(
        self,
        *,
        company,
        founder_account_id: str,
        founder_shares: int,
        additional_authorized: int = 0,
        ipo_price_minor: Optional[int] = None,
    ) -> Result[ShareListing]:
        """让公司上市。

        `founder_shares` 是发起人持有的创世股 —— 对应他的实缴资本，本来就属于他，
        这里只是把「现金出资」换成「股票」形态，公司资产不变。

        `additional_authorized` 是**额外预留**给日后增发的额度。若不预留，
        授权股本会被创世股一次占满，股票就只能发行一次、再也没法增发。
        """
        if self.companies is not None and not self.companies.can_operate(company, founder_account_id):
            return Result.failure(ErrorCode.PERMISSION_DENIED, "只有法人代表能让公司上市")

        existing = self.repo.get_listing(company.account_id)
        if existing is not None and existing.status != ShareStatus.DELISTED.value:
            return Result.failure(ErrorCode.DUPLICATE_TX, "这家公司已经上市了")

        shares = int(founder_shares)
        if shares <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "创世股数量必须大于 0")
        reserved = max(0, int(additional_authorized))
        authorized = shares + reserved

        capital = self.economy.get_balance(company.account_id).balance_minor
        if capital < self.min_capital_minor():
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"公司净资产不足，上市至少需要 {format_amount(self.min_capital_minor())}",
            )

        # 发行价由「实缴资本 / 创世股」决定，保证每股净资产与公司现金一致
        price = int(ipo_price_minor) if ipo_price_minor else max(1, capital // shares)
        if price < self.min_price_minor():
            return Result.failure(
                ErrorCode.INVALID_AMOUNT,
                f"发行价过低（算得 {format_amount(price)}，下限 {format_amount(self.min_price_minor())}）。"
                f"可以减少创世股数量或明确指定发行价",
            )

        try:
            with self.db.transaction():
                listing = self.repo.create_listing(
                    company_account_id=company.account_id,
                    authorized_shares=authorized,
                    ipo_price_minor=price,
                    initial_circulating=shares,
                )
                self.repo.upsert_holding(
                    company_account_id=company.account_id,
                    holder_account_id=founder_account_id,
                    shares=shares,
                    avg_cost_minor=price,
                )
        except Exception as e:
            if self.log:
                self.log.error(f"公司上市失败: {e}")
            return Result.failure(ErrorCode.STORAGE_ERROR, str(e))

        if self.log:
            self.log.info(
                f"公司 {company.name} 上市：创世股 {shares} 股（发行价 {format_amount(price)}），"
                f"另预留 {reserved} 股供增发"
            )
        return Result.success(self.repo.get_listing(company.account_id))

    def delist(self, company, *, reason: str = "manual") -> Result[None]:
        listing = self.repo.get_listing(company.account_id)
        if listing is None:
            return Result.failure(ErrorCode.SHOP_NOT_FOUND, "该公司未上市")
        self.repo.set_status(company.account_id, ShareStatus.DELISTED.value)
        if self.log:
            self.log.warning(f"公司 {company.name} 退市（{reason}）")
        return Result.success(None)

    # ==================== 报价 ====================

    def quote(self, company_account_id: str) -> Optional[Quote]:
        listing = self.repo.get_listing(company_account_id)
        if listing is None:
            return None
        return Quote(
            company_account_id=company_account_id,
            price_minor=listing.last_price_minor,
            nav_per_share_minor=listing.nav_per_share_minor,
            nav_minor=listing.nav_minor,
            circulating_shares=listing.circulating_shares,
            rand_bps=listing.rand_bps,
            price_version=listing.price_version,
            status=listing.status,
        )

    def quote_by_name(self, name: str) -> Optional[Quote]:
        if self.companies is None:
            return None
        company = self.companies.repo.get_by_name(name)
        if company is None:
            return None
        return self.quote(company.account_id)

    # ==================== 定价周期 ====================

    def update_prices(self, *, now: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
        """按周期重算所有上市公司的股价。每小时一次。"""
        current = int(now if now is not None else time.time() * 1000)
        bucket = (current // HOUR_MS) * HOUR_MS
        cfg = self.valuation_config()
        updated: List[Dict[str, Any]] = []

        for listing in self.repo.listed_all():
            if listing.status != ShareStatus.LISTED.value:
                continue
            company = None
            if self.companies is not None:
                company = self.companies.repo.get_by_account(listing.company_account_id)
            if company is None:
                continue

            nav = self.companies.nav_minor(company)
            price, per_share, epsilon = compute_price(
                nav_minor=nav,
                circulating_shares=listing.circulating_shares,
                prev_price_minor=listing.last_price_minor,
                ipo_price_minor=listing.ipo_price_minor,
                cfg=cfg,
                rng=self._rng,
            )
            try:
                with self.db.transaction():
                    self.repo.update_listing(
                        listing.company_account_id,
                        {
                            "nav_minor": nav,
                            "nav_per_share_minor": per_share,
                            "last_price_minor": price,
                            "rand_bps": epsilon,
                            "price_version": listing.price_version + 1,
                        },
                    )
                    self.repo.record_price(
                        company_account_id=listing.company_account_id,
                        bucket_ms=bucket,
                        nav_minor=nav,
                        circulating_shares=listing.circulating_shares,
                        nav_per_share_minor=per_share,
                        rand_bps=epsilon,
                        price_minor=price,
                    )
            except Exception as e:
                if self.log:
                    self.log.error(f"更新 {company.name} 股价失败: {e}")
                continue

            updated.append(
                {
                    "company": company.name,
                    "nav_minor": nav,
                    "nav_per_share_minor": per_share,
                    "price_minor": price,
                    "rand_bps": epsilon,
                }
            )
        return updated

    # ==================== 申购 ====================

    def issue(
        self, *, company_account_id: str, buyer_account_id: str, shares: int, max_price_minor: Optional[int] = None
    ) -> Result[OrderResult]:
        """申购新股：钱进公司账户，同时增加流通股。两步必须同事务，否则就是稀释老股东。"""
        shares = int(shares)
        if shares <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        listing = self.repo.get_listing(company_account_id)
        if listing is None:
            return Result.failure(ErrorCode.SHOP_NOT_FOUND, "该公司未上市")
        if listing.status != ShareStatus.LISTED.value:
            return Result.failure(ErrorCode.SHOP_NOT_ACTIVE, "该公司股票当前不可交易")
        if listing.circulating_shares + shares > listing.authorized_shares:
            remaining = max(0, listing.authorized_shares - listing.circulating_shares)
            return Result.failure(
                ErrorCode.SUPPLY_LIMIT, f"超出授权股本，最多还能申购 {remaining} 股"
            )

        price = listing.last_price_minor
        if price <= 0:
            return Result.failure(ErrorCode.PRICE_NOT_SET, "当前没有有效报价")
        if max_price_minor is not None and price > int(max_price_minor):
            return Result.failure(ErrorCode.PRICE_STALE, "价格已变动，请重新确认")

        amount = shares * price
        buyer = self.economy.repo.get_account(buyer_account_id)
        if buyer is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if buyer.available_minor < amount:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"余额不足，需要 {format_amount(amount)}，当前可用 {format_amount(buyer.available_minor)}",
            )

        try:
            with self.db.transaction():
                # 钱进公司账户
                paid = self.economy.post_in_transaction(
                    kind=TxKind.SHARE_ISSUE.value,
                    entries=[
                        Entry(buyer_account_id, ROLE_PAYER, -amount),
                        Entry(company_account_id, ROLE_PAYEE, amount),
                    ],
                    idem=f"share-issue:{company_account_id}:{new_uuid7()}",
                    tx_id=f"issue:{new_uuid7()}",
                    actor=buyer_account_id,
                    correlation_id=company_account_id,
                    gross=amount,
                    net=amount,
                )
                if not paid.ok:
                    raise AimarketError(paid.code, paid.message)

                # 同一事务内增加流通股 —— 这一行与上一行必须绑在一起
                self.repo.update_listing(
                    company_account_id,
                    {
                        "circulating_shares": listing.circulating_shares + shares,
                        "price_version": listing.price_version + 1,
                    },
                )
                holding = self.repo.get_holding(company_account_id, buyer_account_id)
                old_shares = holding.shares if holding else 0
                old_cost = holding.avg_cost_minor if holding else 0
                new_shares = old_shares + shares
                # 加权平均成本
                new_cost = (
                    (old_shares * old_cost + shares * price) // new_shares
                    if new_shares > 0
                    else price
                )
                self.repo.upsert_holding(
                    company_account_id=company_account_id,
                    holder_account_id=buyer_account_id,
                    shares=new_shares,
                    avg_cost_minor=new_cost,
                )
                self.repo.add_order(
                    company_account_id=company_account_id,
                    holder_account_id=buyer_account_id,
                    side="issue",
                    shares=shares,
                    unit_price_minor=price,
                    amount_minor=amount,
                    ledger_tx_id=paid.value.tx_id,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        return Result.success(
            OrderResult(
                ok=True,
                side="issue",
                shares=shares,
                unit_price_minor=price,
                amount_minor=amount,
                new_holding=new_shares,
                message=f"申购 {shares} 股，单价 {format_amount(price)}",
            )
        )

    # ==================== 赎回 ====================

    def redeem(self, *, company_account_id: str, seller_account_id: str, shares: int) -> Result[OrderResult]:
        """赎回：公司用账户现金回购并注销股份。现金不足直接拒绝，不排队超发。"""
        shares = int(shares)
        if shares <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        listing = self.repo.get_listing(company_account_id)
        if listing is None:
            return Result.failure(ErrorCode.SHOP_NOT_FOUND, "该公司未上市")
        if listing.status != ShareStatus.LISTED.value:
            return Result.failure(ErrorCode.SHOP_NOT_ACTIVE, "该公司股票当前不可交易")

        holding = self.repo.get_holding(company_account_id, seller_account_id)
        if holding is None or holding.shares < shares:
            return Result.failure(
                ErrorCode.ITEM_NOT_ENOUGH,
                f"持股不足，你只有 {holding.shares if holding else 0} 股",
            )

        price = listing.last_price_minor
        amount = shares * price
        company_balance = self.economy.get_balance(company_account_id).balance_minor
        if company_balance < amount:
            return Result.failure(
                ErrorCode.SHOP_NO_BUDGET,
                f"公司现金不足以回购（需 {format_amount(amount)}，公司有 {format_amount(company_balance)}）",
            )

        try:
            with self.db.transaction():
                paid = self.economy.post_in_transaction(
                    kind=TxKind.SHARE_REDEEM.value,
                    entries=[
                        Entry(company_account_id, ROLE_PAYER, -amount),
                        Entry(seller_account_id, ROLE_PAYEE, amount),
                    ],
                    idem=f"share-redeem:{company_account_id}:{new_uuid7()}",
                    tx_id=f"redeem:{new_uuid7()}",
                    actor=seller_account_id,
                    correlation_id=company_account_id,
                    gross=amount,
                    net=amount,
                )
                if not paid.ok:
                    raise AimarketError(paid.code, paid.message)

                self.repo.update_listing(
                    company_account_id,
                    {
                        "circulating_shares": max(0, listing.circulating_shares - shares),
                        "price_version": listing.price_version + 1,
                    },
                )
                left = holding.shares - shares
                if left > 0:
                    self.repo.upsert_holding(
                        company_account_id=company_account_id,
                        holder_account_id=seller_account_id,
                        shares=left,
                        avg_cost_minor=holding.avg_cost_minor,
                    )
                else:
                    self.repo.delete_holding(company_account_id, seller_account_id)
                self.repo.add_order(
                    company_account_id=company_account_id,
                    holder_account_id=seller_account_id,
                    side="redeem",
                    shares=shares,
                    unit_price_minor=price,
                    amount_minor=amount,
                    ledger_tx_id=paid.value.tx_id,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        return Result.success(
            OrderResult(
                ok=True,
                side="redeem",
                shares=shares,
                unit_price_minor=price,
                amount_minor=amount,
                new_holding=left,
                message=f"赎回 {shares} 股，单价 {format_amount(price)}",
            )
        )

    # ==================== 分红 ====================

    def pay_dividend(
        self, *, company, actor_account_id: str, amount_minor: Optional[int] = None, idem: Optional[str] = None
    ) -> Result[DividendResult]:
        """按持股比例分红。公司现金不足或没有流通股就不发。"""
        if self.companies is not None and not self.companies.can_operate(company, actor_account_id):
            return Result.failure(ErrorCode.PERMISSION_DENIED, "只有法人代表能决定分红")

        # 募资锁定期内禁止分红：否则公司可以刚募到钱就立刻分给持股最多的发起人，
        # 「锁定期」这道闸门就形同虚设了。
        if getattr(company, "is_fundraising_locked", False):
            hours = max(1, (company.fundraising_locked_until_ms - now_ms()) // HOUR_MS + 1)
            return Result.failure(
                ErrorCode.TX_BUSY,
                f"公司处于募资锁定期内（还剩约 {hours} 小时），暂时不能分红",
            )

        listing = self.repo.get_listing(company.account_id)
        if listing is None or listing.circulating_shares <= 0:
            return Result.failure(ErrorCode.SHOP_NOT_FOUND, "公司没有可分配的股份")

        shareholders = self.repo.shareholders_of(company.account_id)
        if not shareholders:
            return Result.failure(ErrorCode.ITEM_NOT_ENOUGH, "公司没有股东")

        balance = self.economy.get_balance(company.account_id).balance_minor
        usable = max(0, balance - company.reserve_minor)
        if usable <= 0:
            return Result.failure(
                ErrorCode.SHOP_NO_BUDGET,
                f"公司可分配现金不足（余额 {format_amount(balance)}，需保留运营金 "
                f"{format_amount(company.reserve_minor)}）",
            )

        if amount_minor is None:
            amount_minor = apply_bps(usable, company.dividend_bps)
        total = min(int(amount_minor), usable)
        if total <= 0:
            return Result.failure(ErrorCode.SHOP_NO_BUDGET, "可分配金额为 0")

        tax_bps = self.default_tax_bps()
        tax = apply_bps(total, tax_bps)
        net_total = total - tax
        per_share = net_total // listing.circulating_shares if listing.circulating_shares else 0
        if per_share <= 0:
            return Result.failure(
                ErrorCode.INVALID_AMOUNT, "每股可分金额过低（不足 1 个最小单位），请提高分红总额"
            )

        key = idem or f"dividend:{company.business_id}:{now_ms()}"
        if self.repo.find_dividend(key) is not None:
            return Result.failure(ErrorCode.DUPLICATE_TX, "这次分红已经执行过了")

        total_shares = sum(h.shares for h in shareholders)
        entries = [Entry(company.account_id, ROLE_PAYER, -total)]
        distributed = 0
        paid_count = 0
        for index, holding in enumerate(shareholders):
            if index == len(shareholders) - 1:
                cut = net_total - distributed
            else:
                cut = (net_total * holding.shares) // total_shares
            if cut <= 0:
                continue
            entries.append(Entry(holding.holder_account_id, f"dv{index}", cut))
            distributed += cut
            paid_count += 1
        if tax > 0:
            entries.append(Entry(TREASURY_ACCOUNT_ID, "dividend_tax", tax))

        try:
            with self.db.transaction():
                receipt = self.economy.post_in_transaction(
                    kind=TxKind.COMPANY_DIVIDEND.value,
                    entries=entries,
                    idem=key,
                    tx_id=f"dividend:{new_uuid7()}",
                    actor=company.account_id,
                    correlation_id=company.business_id,
                    gross=total,
                    net=net_total,
                    tax=tax,
                )
                if not receipt.ok:
                    raise AimarketError(receipt.code, receipt.message)
                self.repo.add_dividend(
                    company_account_id=company.account_id,
                    per_share_minor=per_share,
                    total_minor=total,
                    shareholder_count=paid_count,
                    tax_minor=tax,
                    idem=key,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        return Result.success(
            DividendResult(
                ok=True,
                total_minor=total,
                per_share_minor=per_share,
                shareholder_count=paid_count,
                tax_minor=tax,
                message=f"分红 {format_amount(total)}，每股 {format_amount(per_share)}",
            )
        )

    # ==================== 查询 ====================

    def listing_of(self, company_account_id: str) -> Optional[ShareListing]:
        return self.repo.get_listing(company_account_id)

    def all_listings(self, *, only_listed: bool = True) -> List[ShareListing]:
        return self.repo.listed_all(only_listed=only_listed)

    def holdings_of(self, holder_account_id: str) -> List[Holding]:
        return self.repo.holdings_of_holder(holder_account_id)

    def shareholders_of(self, company_account_id: str) -> List[Holding]:
        return self.repo.shareholders_of(company_account_id)

    def total_market_cap(self) -> int:
        return self.repo.total_market_cap()

    def portfolio_summary(self, holder_account_id: str) -> Dict[str, Any]:
        holdings = self.repo.holdings_of_holder(holder_account_id)
        value = sum(h.market_value_minor for h in holdings)
        cost = sum(h.cost_basis_minor for h in holdings)
        return {
            "holdings": holdings,
            "count": len(holdings),
            "market_value_minor": value,
            "cost_basis_minor": cost,
            "profit_minor": value - cost,
        }

    def describe_listing(self, listing: ShareListing, name: str = "") -> str:
        return (
            f"{name or listing.company_account_id[:8]} "
            f"股价 {format_amount(listing.last_price_minor)}（每股净资产 "
            f"{format_amount(listing.nav_per_share_minor)}，随机指数 {listing.rand_bps / 100:+.2f}%）"
            f"｜流通 {listing.circulating_shares}/{listing.authorized_shares} 股"
        )

    # ==================== 行情指标 ====================

    def price_change_bps(self, company_account_id: str, *, periods: int = 1) -> int:
        """相对 periods 期前的涨跌幅（基点）。正数=涨。

        注意 `price_series` 返回的是**时间升序**（最旧在前），
        所以最新价在末尾、基准价在开头 —— 反过来取会得到符号完全相反的涨跌幅。
        """
        points = self.price_series(company_account_id, limit=periods + 1)
        if len(points) < 2:
            return 0
        prior = int(points[0]["price_minor"] or 0)       # 最旧
        latest = int(points[-1]["price_minor"] or 0)     # 最新
        if prior <= 0:
            return 0
        return (latest - prior) * 10000 // prior

    def price_series(self, company_account_id: str, *, limit: int = 24) -> List[Dict[str, Any]]:
        """价格历史，**按时间升序**返回（最旧在前），方便画走势。"""
        rows = self.repo.price_series(company_account_id, limit=limit)
        rows.reverse()
        return rows

    def trend_bar(self, company_account_id: str, *, periods: int = 16, width: int = 24) -> str:
        """把价格历史画成一行文字走势，用不同字符表示相对高低。

        用 ▁▂▃▄▅▆▇█ 八级高度，观感上就是一条迷你 K 线。
        没有历史数据时返回空串，调用方自行决定是否显示。
        """
        points = self.price_series(company_account_id, limit=periods)
        prices = [int(p["price_minor"] or 0) for p in points if int(p["price_minor"] or 0) > 0]
        if len(prices) < 2:
            return ""
        lo, hi = min(prices), max(prices)
        blocks = "▁▂▃▄▅▆▇█"
        if hi == lo:
            return blocks[3] * len(prices)
        out = []
        for price in prices:
            level = (price - lo) * (len(blocks) - 1) // max(1, hi - lo)
            out.append(blocks[max(0, min(len(blocks) - 1, level))])
        return "".join(out)

    def fundamentals(self, company) -> Dict[str, Any]:
        """公司基本面：投资人决定买不买该看的东西。"""
        listing = self.repo.get_listing(company.account_id)
        assets = self.companies.assets(company) if self.companies else None
        holdings = self.repo.shareholders_of(company.account_id) if listing else []
        dividends = self.repo.dividends_of(company.account_id, limit=5) if listing else []
        return {
            "listing": listing,
            "assets": assets,
            "nav_minor": assets.nav_minor if assets else 0,
            "shareholders": len(holdings),
            "top_holders": holdings[:5],
            "dividend_count": len(dividends),
            "last_dividend": dividends[0] if dividends else None,
            "change_bps": self.price_change_bps(company.account_id) if listing else 0,
            "trend": self.trend_bar(company.account_id) if listing else "",
            "market_cap_minor": listing.market_cap_minor if listing else 0,
        }

    def my_position(self, holder_account_id: str, company_account_id: str) -> Dict[str, Any]:
        holding = self.repo.get_holding(company_account_id, holder_account_id)
        if holding is None:
            return {"shares": 0, "value_minor": 0, "cost_minor": 0, "profit_minor": 0, "profit_bps": 0}
        value = holding.market_value_minor
        cost = holding.cost_basis_minor
        profit = value - cost
        return {
            "shares": holding.shares,
            "avg_cost_minor": holding.avg_cost_minor,
            "value_minor": value,
            "cost_minor": cost,
            "profit_minor": profit,
            "profit_bps": (profit * 10000 // cost) if cost > 0 else 0,
        }
