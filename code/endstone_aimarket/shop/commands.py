"""命令处理。

命令清单
    /shop            打开商店主菜单（玩家）
    /shopadmin       管理命令（OP）
    /money, /bal     查看余额与最近流水
    /pay <玩家> <金额> 转账给在线玩家
"""
from __future__ import annotations

from typing import List

from endstone.command import Command, CommandSender

from ..core.ids import short_id
from ..core.money import format_amount, parse_amount

COMMANDS = {
    "shop": {
        "description": "打开商店菜单",
        "usages": ["/shop"],
        "aliases": ["商店"],
        "permissions": ["aimarket.command.shop"],
    },
    "shopadmin": {
        "description": "aimarket 管理命令",
        "usages": [
            "/shopadmin",
            "/shopadmin reload",
            "/shopadmin audit",
            "/shopadmin pending",
            "/shopadmin resolve <id: str> <action: str>",
            "/shopadmin give <player: str> <amount: float>",
            "/shopadmin take <player: str> <amount: float>",
            "/shopadmin resetprices",
            "/shopadmin daily",
        ],
        "permissions": ["aimarket.command.admin"],
    },
    "money": {
        "description": "查看余额与流水",
        "usages": ["/money", "/money <player: str>"],
        "aliases": ["bal", "余额"],
        "permissions": ["aimarket.command.money"],
    },
    "pay": {
        "description": "给其他玩家转账",
        "usages": ["/pay <player: str> <amount: float>"],
        "aliases": ["转账"],
        "permissions": ["aimarket.command.pay"],
    },
    "biz": {
        "description": "公司与贷款",
        "usages": [
            "/biz",
            "/biz create <name: str> <capital: float>",
            "/biz info [name: str]",
            "/biz list",
            "/biz addfund <amount: float>",
            "/biz list_stock <genesis: int> [reserved: int]",
            "/biz dividend <amount: float>",
            "/biz liquidate",
            "/biz borrow <amount: float> [days: int]",
            "/biz repay [amount: float]",
            "/biz debt",
        ],
        "aliases": ["公司", "loan", "贷款"],
        "permissions": ["aimarket.command.biz"],
    },
    "stock": {
        "description": "股票行情与买卖",
        "usages": [
            "/stock",
            "/stock list",
            "/stock info <name: str>",
            "/stock buy <name: str> <shares: int>",
            "/stock sell <name: str> <shares: int>",
            "/stock my",
        ],
        "aliases": ["股票", "股市"],
        "permissions": ["aimarket.command.stock"],
    },
    "hud": {
        "description": "设置侧边栏/头顶显示什么",
        "usages": ["/hud", "/hud toggle <item: str>"],
        "aliases": ["display", "显示"],
        "permissions": ["aimarket.command.hud"],
    },
}

PERMISSIONS = {
    "aimarket.command.shop": {"description": "允许使用 /shop", "default": True},
    "aimarket.command.money": {"description": "允许使用 /money", "default": True},
    "aimarket.command.pay": {"description": "允许使用 /pay", "default": True},
    "aimarket.command.biz": {"description": "允许使用 /biz", "default": True},
    "aimarket.command.stock": {"description": "允许使用 /stock", "default": True},
    "aimarket.command.hud": {"description": "允许使用 /hud", "default": True},
    "aimarket.command.admin": {"description": "允许使用 /shopadmin", "default": "op"},
}


class ShopCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 分发 ====================

    def handle(self, sender: CommandSender, command: Command, args: List[str]) -> bool:
        # 冲突改名后的命令 → 原始处理分支
        name = self.plugin.command_name_map.get(command.name, command.name)
        if name == "shop":
            return self._shop(sender)
        if name == "shopadmin":
            return self._admin(sender, args)
        if name == "money":
            return self._money(sender, args)
        if name == "pay":
            return self._pay(sender, args)
        if name == "biz":
            return self._biz(sender, args)
        if name == "stock":
            return self._stock(sender, args)
        if name == "hud":
            return self._hud(sender, args)
        return False

    # ==================== /biz 公司与贷款 ====================

    def _biz(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        account = plugin.economy_api.api_ensure_player(sender)
        if account is None:
            sender.send_message(plugin.text("err.account_not_found"))
            return False

        if not args:
            self._biz_menu(sender, account)
            return True

        sub = args[0].lower()
        if sub == "create":
            if len(args) < 3:
                sender.send_message(plugin.text("biz.usage_create"))
                return False
            try:
                capital = parse_amount(args[2])
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            result = plugin.company_service.register(
                founder_account_id=account.account_id, name=args[1], capital_minor=capital
            )
            sender.send_message(
                plugin.text("biz.created", name=args[1], capital=format_amount(capital))
                if result.ok else plugin.err_message(result)
            )
            return True

        if sub == "list":
            companies = plugin.company_service.list_all()
            sender.send_message(plugin.text("biz.list_header", n=len(companies)))
            for company in companies[:20]:
                sender.send_message(
                    plugin.text(
                        "biz.list_line",
                        name=company.name,
                        nav=format_amount(plugin.company_service.nav_minor(company)),
                        status=company.status,
                    )
                )
            return True

        if sub == "info":
            company = plugin.company_service.find(args[1]) if len(args) > 1 else self._my_company(account)
            if company is None:
                sender.send_message(plugin.text("biz.not_found"))
                return False
            summary = plugin.company_service.summary(company)
            ab = summary["assets"]
            sender.send_message(plugin.text("biz.info_header", name=company.name))
            sender.send_message(plugin.text("biz.info_assets", detail=ab.describe()))
            sender.send_message(plugin.text("biz.info_shops", n=ab.shop_count, members=summary["member_count"]))
            sender.send_message(plugin.text("biz.info_tax", bps=company.turnover_tax_bps / 100))
            debt = plugin.loan_service.total_debt_of(company.account_id)
            sender.send_message(plugin.text("biz.info_debt", value=format_amount(debt)))
            credit = plugin.loan_service.credit_limit(company.account_id)
            sender.send_message(plugin.text("biz.info_credit", value=format_amount(credit)))
            return True

        if sub == "addfund":
            if len(args) < 2:
                sender.send_message(plugin.text("biz.usage_addfund"))
                return False
            company = self._require_founder(sender, account)
            if company is None:
                return False
            try:
                amount = parse_amount(args[1])
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            result = plugin.economy_service.transfer(
                account.account_id, company.account_id, amount,
                reason="company_addfund", idem=f"addfund:{company.business_id}:{short_id(8)}",
            )
            if result.ok:
                plugin.company_service.repo.update(
                    company.business_id,
                    {"registered_capital_minor": company.registered_capital_minor + amount},
                )
                sender.send_message(plugin.text("biz.addfund_done", value=format_amount(amount)))
            else:
                sender.send_message(plugin.err_message(result))
            return True

        if sub == "list_stock":
            if len(args) < 2:
                sender.send_message(plugin.text("biz.usage_list_stock"))
                return False
            company = self._require_founder(sender, account)
            if company is None:
                return False
            try:
                founder_shares = int(args[1])
                reserved = int(args[2]) if len(args) > 2 else 0
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            result = plugin.stock_service.list_company(
                company=company, founder_account_id=account.account_id,
                founder_shares=founder_shares, additional_authorized=reserved,
            )
            if result.ok:
                listing = result.value
                sender.send_message(
                    plugin.text(
                        "biz.listed", name=company.name, shares=listing.circulating_shares,
                        price=format_amount(listing.ipo_price_minor),
                        authorized=listing.authorized_shares,
                    )
                )
            else:
                sender.send_message(plugin.err_message(result))
            return True

        if sub == "dividend":
            if len(args) < 2:
                sender.send_message(plugin.text("biz.usage_dividend"))
                return False
            company = self._require_founder(sender, account)
            if company is None:
                return False
            try:
                amount = parse_amount(args[1])
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            result = plugin.stock_service.pay_dividend(
                company=company, actor_account_id=account.account_id, amount_minor=amount
            )
            if result.ok:
                d = result.value
                sender.send_message(
                    plugin.text(
                        "biz.dividend_done", total=format_amount(d.total_minor),
                        per_share=format_amount(d.per_share_minor),
                        n=d.shareholder_count, tax=format_amount(d.tax_minor),
                    )
                )
            else:
                sender.send_message(plugin.err_message(result))
            return True

        if sub == "liquidate":
            company = self._require_founder(sender, account)
            if company is None:
                return False
            plan = plugin.company_service.plan_liquidation(company)
            sender.send_message(plugin.text("biz.liquidate_plan"))
            sender.send_message("§7" + plan.describe().replace("\n", "\n§7"))
            result = plugin.company_service.liquidate(company, reason="manual")
            if result.ok:
                sender.send_message(plugin.text("biz.liquidate_done", name=company.name))
            else:
                sender.send_message(plugin.err_message(result))
            return True

        # ---------- 出资给别的公司：投资 / 融资 / 赞助 ----------
        if sub in ("invest", "finance", "sponsor"):
            if len(args) < 3:
                sender.send_message(plugin.text("fund.usage_contribute"))
                return False
            target = plugin.company_service.find(args[1])
            if target is None:
                sender.send_message(plugin.text("biz.not_found"))
                return False
            try:
                amount = parse_amount(args[2])
                days = int(args[3]) if len(args) > 3 else 1
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False

            result = plugin.fundraising_service.contribute(
                company=target, funder_account_id=account.account_id,
                kind=sub, amount_minor=amount, days=days,
            )
            if not result.ok:
                sender.send_message(plugin.err_message(result))
                return True
            data = result.value
            if sub == "invest":
                sender.send_message(
                    plugin.text("fund.invest_done", name=target.name,
                                shares=data["shares"],
                                price=format_amount(data["unit_price_minor"]),
                                amount=format_amount(data["amount_minor"]))
                )
            elif sub == "finance":
                sender.send_message(
                    plugin.text("fund.finance_done", name=target.name,
                                amount=format_amount(data["amount_minor"]),
                                rate=data["rate_bps_per_day"] / 100,
                                term=data["term_days"],
                                interest=format_amount(data["expected_interest_minor"]))
                )
            else:
                sender.send_message(
                    plugin.text("fund.sponsor_done", name=target.name,
                                amount=format_amount(data["amount_minor"]),
                                days=data["days"])
                )
            return True

        # ---------- 查看某公司怎么募资、募到了多少 ----------
        if sub == "fund":
            target = plugin.company_service.find(args[1]) if len(args) > 1 else self._my_company(account)
            if target is None:
                sender.send_message(plugin.text("biz.not_found"))
                return False
            fund = plugin.fundraising_service
            breakdown = fund.breakdown(target.account_id)
            sender.send_message(plugin.text("fund.header", name=target.name))
            sender.send_message(
                plugin.text("fund.breakdown",
                            invest=format_amount(breakdown.get("invest", 0)),
                            finance=format_amount(breakdown.get("finance", 0)),
                            sponsor=format_amount(breakdown.get("sponsor", 0)),
                            total=format_amount(target.total_raised_minor))
            )
            sender.send_message(
                plugin.text("fund.terms",
                            rate=target.finance_rate_bps_per_day / 100,
                            term=target.finance_term_days)
            )
            if fund.is_locked(target):
                sender.send_message(
                    plugin.text("fund.locked", hours=fund.lock_remaining_hours(target))
                )
            sponsors = fund.active_sponsors(target.account_id)
            if sponsors:
                sender.send_message(
                    plugin.text("fund.sponsors",
                                names="、".join(s.display_name or "?" for s in sponsors[:5]))
                )
            records = fund.records_of_company(target.account_id, limit=5)
            if records:
                sender.send_message(plugin.text("fund.recent_header"))
                for rec in records:
                    sender.send_message(
                        plugin.text("fund.recent_line",
                                    kind=rec.kind_name,
                                    who=rec.funder_name or rec.funder_account_id[:8],
                                    value=format_amount(rec.amount_minor))
                    )
            return True

        if sub == "borrow":
            if len(args) < 2:
                sender.send_message(plugin.text("biz.usage_borrow"))
                return False
            try:
                amount = parse_amount(args[1])
                term = int(args[2]) if len(args) > 2 else None
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            company = self._my_company(account)
            borrower = company.account_id if company is not None else account.account_id
            result = plugin.loan_service.borrow(
                borrower_account_id=borrower, amount_minor=amount, term_days=term
            )
            if result.ok:
                loan = result.value
                sender.send_message(
                    plugin.text(
                        "biz.borrow_done", value=format_amount(amount),
                        term=loan.term_days, rate=loan.rate_bps_per_day / 100,
                    )
                )
            else:
                sender.send_message(plugin.err_message(result))
            return True

        if sub == "repay":
            loans = plugin.loan_service.loans_of(account.account_id)
            company = self._my_company(account)
            if company is not None:
                loans += plugin.loan_service.loans_of(company.account_id)
            if not loans:
                sender.send_message(plugin.text("biz.no_loan"))
                return True
            loan = min(loans, key=lambda l: l.due_at_ms)
            if len(args) > 1:
                try:
                    amount = parse_amount(args[1])
                except ValueError:
                    sender.send_message(plugin.text("err.invalid_amount"))
                    return False
            else:
                amount = loan.total_due_minor
            result = plugin.loan_service.repay(loan_id=loan.loan_id, amount_minor=amount)
            if result.ok:
                sender.send_message(
                    plugin.text(
                        "biz.repay_done",
                        value=format_amount(result.value["repaid_minor"]),
                        left=format_amount(result.value["remaining_minor"]),
                        settled=plugin.text("biz.settled") if result.value["settled"] else "",
                    )
                )
            else:
                sender.send_message(plugin.err_message(result))
            return True

        if sub == "debt":
            loans = plugin.loan_service.loans_of(account.account_id)
            company = self._my_company(account)
            if company is not None:
                loans += plugin.loan_service.loans_of(company.account_id)
            if not loans:
                sender.send_message(plugin.text("biz.no_loan"))
                return True
            sender.send_message(plugin.text("biz.debt_header", n=len(loans)))
            for loan in loans:
                sender.send_message(
                    plugin.text(
                        "biz.debt_line", status=loan.status,
                        detail=plugin.loan_service.describe(loan),
                        grace=loan.grace_until_ms // 1000,
                    )
                )
            return True

        self._biz_menu(sender, account)
        return True

    def _biz_menu(self, sender, account) -> None:
        plugin = self.plugin
        company = self._my_company(account)
        sender.send_message(plugin.text("biz.help_header"))
        if company is None:
            sender.send_message(plugin.text("biz.help_no_company"))
            sender.send_message("§7  /biz create <公司名> <注册资本>")
        else:
            nav = plugin.company_service.nav_minor(company)
            sender.send_message(
                plugin.text("biz.help_company", name=company.name, nav=format_amount(nav))
            )
            for line in (
                "/biz info", "/biz addfund <金额>", "/biz list_stock <创世股> [预留股]",
                "/biz dividend <金额>", "/biz liquidate",
            ):
                sender.send_message(f"§7  {line}")
        for line in (
            "/biz invest <公司名> <金额>     投资换股票",
            "/biz finance <公司名> <金额>    融资换债权（到期还本付息）",
            "/biz sponsor <公司名> <金额> [天数]  赞助换广告位",
            "/biz fund [公司名]              查看募资情况",
            "/biz borrow <金额> [天数]", "/biz repay [金额]", "/biz debt", "/biz list",
        ):
            sender.send_message(f"§7  {line}")

    def _my_company(self, account):
        plugin = self.plugin
        if plugin.company_service is None:
            return None
        owned = [
            c for c in plugin.company_service.companies_of(account.account_id)
            if c.founder_account_id == account.account_id and c.is_active
        ]
        return owned[0] if owned else None

    def _require_founder(self, sender, account):
        company = self._my_company(account)
        if company is None:
            sender.send_message(plugin.text("biz.not_found"))
        return company

    # ==================== /stock 股票 ====================

    def _stock(self, sender, args) -> bool:
        plugin = self.plugin
        service = plugin.stock_service
        if service is None:
            sender.send_message(plugin.text("ui.stock.none"))
            return False

        sub = args[0].lower() if args else "market"

        if sub in ("market", "gui", "面板") and plugin.is_player(sender):
            plugin.stock_forms.open_market(sender)
            return True

        if sub == "list":
            listings = service.all_listings()
            sender.send_message(plugin.text("stock.list_header", n=len(listings)))
            for listing in listings[:20]:
                company = service.companies.repo.get_by_account(listing.company_account_id)
                name = company.name if company else listing.company_account_id[:8]
                sender.send_message("§7  " + service.describe_listing(listing, name))
            if not listings:
                sender.send_message(plugin.text("stock.none"))
            return True

        if not plugin.is_player(sender):
            # 控制台只能用文本形式
            return self._stock(sender, ["list"])
        account = plugin.economy_api.api_ensure_player(sender)
        if account is None:
            return False

        if sub == "info":
            if len(args) < 2:
                sender.send_message(plugin.text("stock.usage_info"))
                return False
            company = service.companies.repo.get_by_name(args[1])
            if company is None:
                sender.send_message(plugin.text("biz.not_found"))
                return False
            listing = service.listing_of(company.account_id)
            if listing is None:
                sender.send_message(plugin.text("stock.not_listed"))
                return False
            sender.send_message(plugin.text("stock.info_header", name=company.name))
            sender.send_message("§7  " + service.describe_listing(listing, company.name))
            for holding in service.shareholders_of(company.account_id)[:10]:
                sender.send_message(
                    plugin.text(
                        "stock.holder_line",
                        name=holding.holder_name or holding.holder_account_id[:8],
                        shares=holding.shares,
                    )
                )
            return True

        if sub in ("buy", "sell"):
            if len(args) < 3:
                sender.send_message(plugin.text("stock.usage_trade"))
                return False
            company = service.companies.repo.get_by_name(args[1])
            if company is None:
                sender.send_message(plugin.text("biz.not_found"))
                return False
            try:
                shares = int(args[2])
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            if sub == "buy":
                result = service.issue(
                    company_account_id=company.account_id,
                    buyer_account_id=account.account_id,
                    shares=shares,
                )
                if result.ok:
                    o = result.value
                    sender.send_message(
                        plugin.text("stock.bought", shares=o.shares,
                                    price=format_amount(o.unit_price_minor),
                                    total=format_amount(o.amount_minor),
                                    holding=o.new_holding)
                    )
                else:
                    sender.send_message(plugin.err_message(result))
            else:
                result = service.redeem(
                    company_account_id=company.account_id,
                    seller_account_id=account.account_id,
                    shares=shares,
                )
                if result.ok:
                    o = result.value
                    sender.send_message(
                        plugin.text("stock.sold", shares=o.shares,
                                    price=format_amount(o.unit_price_minor),
                                    total=format_amount(o.amount_minor),
                                    holding=o.new_holding)
                    )
                else:
                    sender.send_message(plugin.err_message(result))
            return True

        if sub == "my":
            summary = service.portfolio_summary(account.account_id)
            sender.send_message(
                plugin.text(
                    "stock.my_header", n=summary["count"],
                    value=format_amount(summary["market_value_minor"]),
                    profit=format_amount(summary["profit_minor"]),
                )
            )
            for holding in summary["holdings"]:
                sender.send_message(
                    plugin.text(
                        "stock.my_line",
                        name=holding.company_name or holding.company_account_id[:8],
                        shares=holding.shares,
                        value=format_amount(holding.market_value_minor),
                        profit=format_amount(holding.profit_minor),
                    )
                )
            return True

        sender.send_message(plugin.text("stock.help_header"))
        for line in ("/stock           打开股市面板", "/stock list", "/stock info <公司名>",
                     "/stock buy <公司名> <股数>", "/stock sell <公司名> <股数>", "/stock my"):
            sender.send_message(f"§7  {line}")
        return True

    # ==================== /hud 自选显示 ====================

    def _hud(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        if args and args[0].lower() == "toggle" and len(args) > 1:
            ok = plugin.ui_forms.toggle_by_key(sender, args[1])
            sender.send_message(
                plugin.text("ui.prefs.toggled", key=args[1]) if ok
                else plugin.text("ui.prefs.unknown", key=args[1],
                                 keys=", ".join(plugin.ui_forms.list_keys()))
            )
            return True
        plugin.ui_forms.open_main(sender)
        return True

    # ==================== /shop ====================

    def _shop(self, sender) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        plugin.forms.open_main_menu(sender)
        return True

    # ==================== /money ====================

    def _money(self, sender, args) -> bool:
        plugin = self.plugin
        if args and plugin.is_admin(sender):
            target_name = args[0]
            account = plugin.economy_service.repo.get_by_name(target_name)
            if account is None:
                sender.send_message(plugin.text("err.account_not_found"))
                return False
        else:
            if not plugin.is_player(sender):
                sender.send_message(plugin.text("err.not_player"))
                return False
            account = plugin.economy_api.api_ensure_player(sender)
            if account is None:
                sender.send_message(plugin.text("err.account_not_found"))
                return False

        balance = plugin.economy_service.get_balance(account.account_id)
        sender.send_message(
            plugin.text("money.balance", who=account.current_name or account.account_id,
                        value=format_amount(balance.balance_minor))
        )
        if balance.held_minor:
            sender.send_message(plugin.text("money.held", value=format_amount(balance.held_minor)))
        for record in plugin.economy_service.history(account.account_id, limit=5):
            sign = "+" if record.delta_minor >= 0 else "-"
            sender.send_message(
                plugin.text(
                    "money.line",
                    kind=plugin.tx_kind_name(record.kind),
                    amount=f"{sign}{format_amount(abs(record.delta_minor))}",
                )
            )
        return True

    # ==================== /pay ====================

    def _pay(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        if len(args) < 2:
            sender.send_message(plugin.text("pay.usage"))
            return False

        target_name, raw_amount = args[0], args[1]
        try:
            amount = parse_amount(raw_amount)
        except ValueError:
            sender.send_message(plugin.text("err.invalid_amount"))
            return False

        src = plugin.economy_api.api_ensure_player(sender)
        if src is None:
            sender.send_message(plugin.text("err.account_not_found"))
            return False
        dst = plugin.economy_service.repo.get_by_name(target_name)
        if dst is None:
            sender.send_message(plugin.text("err.account_not_found"))
            return False
        if dst.account_id == src.account_id:
            sender.send_message(plugin.text("pay.self"))
            return False

        tax_bps = int(plugin.cfg.as_int("economy.transfer_tax_bps", 0))
        result = plugin.economy_service.transfer_taxed(
            src.account_id,
            dst.account_id,
            amount,
            tax_bps=tax_bps,
            reason=f"transfer:{short_id(8)}",
            actor_account_id=src.account_id,
        )
        if not result.ok:
            sender.send_message(plugin.error_text(result.code, result.message))
            return False

        receipt = result.value
        if receipt.idempotent_replay:
            sender.send_message(plugin.text("pay.duplicate"))
            return True
        sender.send_message(
            plugin.text("pay.sent", who=target_name, value=format_amount(amount - receipt.tax_minor))
        )
        if receipt.tax_minor:
            sender.send_message(plugin.text("pay.tax", value=format_amount(receipt.tax_minor)))
        target = plugin.server.get_player(target_name)
        if target is not None:
            target.send_message(
                plugin.text(
                    "pay.received",
                    who=sender.name,
                    value=format_amount(amount - receipt.tax_minor),
                )
            )
        return True

    # ==================== /shopadmin ====================

    def _admin(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_admin(sender):
            sender.send_message(plugin.text("err.permission_denied"))
            return False
        if not args:
            self._admin_help(sender)
            return True

        sub = args[0].lower()
        if sub == "reload":
            try:
                plugin.reload_all()
                sender.send_message(plugin.text("admin.reloaded"))
            except Exception as e:
                sender.send_message(plugin.text("admin.reload_failed", err=str(e)))
            return True

        if sub == "audit":
            audit = plugin.recovery.audit()
            sender.send_message(plugin.text("admin.audit_header"))
            for key, value in audit.items():
                sender.send_message(plugin.text("admin.audit_line", k=key, v=value))
            return True

        if sub == "pending":
            pending = plugin.coordinator.pending_trades()
            manual = plugin.recovery.list_manual()
            sender.send_message(plugin.text("admin.pending_header", n=len(pending), m=len(manual)))
            for trade in pending + manual:
                sender.send_message(
                    plugin.text(
                        "admin.pending_line",
                        id=trade["trade_id"],
                        status=trade["status"],
                        shop=trade["shop_id"],
                        qty=trade["requested_qty"],
                    )
                )
            return True

        if sub == "resolve":
            if len(args) < 3:
                sender.send_message(plugin.text("admin.resolve_usage"))
                return False
            trade_id, action = args[1], args[2].lower()
            if action not in ("cancel", "confirm"):
                sender.send_message(plugin.text("admin.resolve_usage"))
                return False
            result = plugin.recovery.resolve(trade_id, action=action)
            sender.send_message(result.value if result.ok else plugin.err_message(result))
            return True

        if sub in ("give", "take"):
            if len(args) < 3:
                sender.send_message(plugin.text("admin.adjust_usage"))
                return False
            account = plugin.economy_service.repo.get_by_name(args[1])
            if account is None:
                sender.send_message(plugin.text("err.account_not_found"))
                return False
            try:
                amount = parse_amount(args[2])
            except ValueError:
                sender.send_message(plugin.text("err.invalid_amount"))
                return False
            delta = amount if sub == "give" else -amount
            result = plugin.economy_service.admin_adjust(
                account.account_id, delta, reason=f"admin:{sender.name}"
            )
            if result.ok:
                balance = plugin.economy_service.get_balance(account.account_id)
                sender.send_message(
                    plugin.text(
                        "admin.adjust_done",
                        who=args[1],
                        value=format_amount(abs(delta)),
                        balance=format_amount(balance.balance_minor),
                    )
                )
            else:
                sender.send_message(plugin.error_text(result.code, result.message))
            return True

        if sub == "resetprices":
            n = plugin.pricing.reset_adjustments()
            sender.send_message(plugin.text("admin.reset_prices_done", n=n))
            return True

        if sub == "daily":
            n = plugin.pricing.maybe_apply_daily()
            sender.send_message(plugin.text("admin.daily_done", n=n))
            sender.send_message(plugin.pricing.daily_summary())
            return True

        self._admin_help(sender)
        return True

    def _admin_help(self, sender) -> None:
        plugin = self.plugin
        sender.send_message(plugin.text("admin.help_header"))
        for line in (
            "/shopadmin reload",
            "/shopadmin audit",
            "/shopadmin pending",
            "/shopadmin resolve <交易ID> <cancel|confirm>",
            "/shopadmin give <玩家> <金额>",
            "/shopadmin take <玩家> <金额>",
            "/shopadmin resetprices",
            "/shopadmin daily",
        ):
            sender.send_message(f"§7  {line}")
