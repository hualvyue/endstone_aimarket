"""股市界面：行情列表、个股基本面、买卖、我的持仓。

涨跌颜色遵循中国市场习惯：**涨红、跌绿**。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from endstone.form import ActionForm, ModalForm, TextInput

from ..core.money import format_amount


def _submitted(raw) -> List[Any]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return list(data) if isinstance(data, list) else [data]
        except Exception:
            return []
    return list(raw or [])


def _input(label: str, default: str = "", placeholder: str = "") -> TextInput:
    try:
        return TextInput(label=label, placeholder=placeholder, default_value=str(default))
    except TypeError:
        return TextInput(label=label, default_value=str(default))


def _trend_color(bps: int) -> str:
    """中国市场惯例：涨红、跌绿、平灰。"""
    if bps > 0:
        return "§c"
    if bps < 0:
        return "§a"
    return "§7"


def _pct(bps: int) -> str:
    return f"{bps / 100:+.2f}%"


class StockForms:
    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 行情列表 ====================

    def open_market(self, player) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        if service is None:
            player.send_message(plugin.text("ui.stock.none"))
            return

        listings = service.all_listings()
        account = plugin.economy_api.api_ensure_player(player)

        content = [plugin.text("stock.market.header", n=len(listings))]
        if account is not None:
            summary = service.portfolio_summary(account.account_id)
            content.append(
                plugin.text(
                    "stock.market.myline",
                    value=format_amount(summary["market_value_minor"]),
                    n=summary["count"],
                )
            )
        content.append("")
        if not listings:
            content.append(plugin.text("stock.none"))

        form = ActionForm(title=plugin.text("stock.market.title"), content="\n".join(content))

        for listing in listings[:20]:
            company = service.companies.repo.get_by_account(listing.company_account_id)
            if company is None:
                continue
            change = service.price_change_bps(company.account_id)
            color = _trend_color(change)
            trend = service.trend_bar(company.account_id, periods=12)
            label = plugin.text(
                "stock.market.entry",
                name=company.name,
                price=format_amount(listing.last_price_minor, with_symbol=False),
                color=color,
                change=_pct(change),
                trend=trend,
            )
            form.add_button(
                label, on_click=lambda p, c=company: self.open_detail(p, c)
            )

        form.add_button(plugin.text("stock.market.portfolio"), on_click=lambda p: self.open_portfolio(p))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 个股详情 ====================

    def open_detail(self, player, company) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        if service is None or company is None:
            return

        listing = service.listing_of(company.account_id)
        if listing is None:
            player.send_message(plugin.text("stock.not_listed"))
            return

        data = service.fundamentals(company)
        change = data["change_bps"]
        color = _trend_color(change)

        lines = [
            plugin.text("stock.detail.price",
                        value=format_amount(listing.last_price_minor),
                        color=color, change=_pct(change)),
        ]
        if data["trend"]:
            lines.append(plugin.text("stock.detail.trend", trend=data["trend"]))
        lines += [
            "",
            plugin.text("stock.detail.nav",
                        nav=format_amount(data["nav_minor"]),
                        per=format_amount(listing.nav_per_share_minor)),
            plugin.text("stock.detail.shares",
                        circ=listing.circulating_shares, total=listing.authorized_shares),
            plugin.text("stock.detail.cap", value=format_amount(data["market_cap_minor"])),
            plugin.text("stock.detail.holders", n=data["shareholders"]),
        ]
        assets = data.get("assets")
        if assets is not None:
            lines.append(plugin.text("stock.detail.assets",
                                     cash=format_amount(assets.cash_minor),
                                     stock=format_amount(assets.stock_value_minor),
                                     debt=format_amount(assets.debt_minor)))
        if data["last_dividend"]:
            lines.append(
                plugin.text("stock.detail.last_dividend",
                            value=format_amount(int(data["last_dividend"]["total_minor"])))
            )
        if data["top_holders"]:
            names = "、".join(
                f"{h.holder_name or '?'}({h.shares})" for h in data["top_holders"][:3]
            )
            lines.append(plugin.text("stock.detail.top", names=names))

        account = plugin.economy_api.api_ensure_player(player)
        if account is not None:
            pos = service.my_position(account.account_id, company.account_id)
            if pos["shares"] > 0:
                pcolor = _trend_color(pos["profit_minor"])
                lines.append("")
                lines.append(
                    plugin.text("stock.detail.my_position",
                                shares=pos["shares"],
                                value=format_amount(pos["value_minor"]),
                                color=pcolor,
                                profit=format_amount(pos["profit_minor"]),
                                pct=_pct(pos["profit_bps"]))
                )

        form = ActionForm(title=plugin.text("stock.detail.title", name=company.name),
                          content="\n".join(lines))
        form.add_button(plugin.text("stock.detail.buy"), on_click=lambda p: self.open_buy(p, company))
        if account is not None:
            pos = service.my_position(account.account_id, company.account_id)
            if pos["shares"] > 0:
                form.add_button(plugin.text("stock.detail.sell"), on_click=lambda p: self.open_sell(p, company))
        form.add_button(plugin.text("stock.detail.holders_list"),
                        on_click=lambda p: self.open_holders(p, company))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_market(p))
        player.send_form(form)

    # ==================== 买入 ====================

    def open_buy(self, player, company) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        listing = service.listing_of(company.account_id)
        if listing is None:
            return
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return

        price = listing.last_price_minor
        balance = plugin.economy_service.get_balance(account.account_id).available_minor
        room = max(0, listing.authorized_shares - listing.circulating_shares)
        affordable = balance // max(1, price)
        max_shares = min(room, affordable)

        form = ModalForm(
            title=plugin.text("stock.buy.title", name=company.name),
            content=plugin.text(
                "stock.buy.hint",
                price=format_amount(price),
                balance=format_amount(balance),
                max=max_shares,
            ),
        )
        form.add_control(_input(plugin.text("stock.buy.shares"), "1", "1"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                shares = int(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            if shares <= 0:
                p.send_message(plugin.text("err.invalid_amount"))
                return
            result = service.issue(
                company_account_id=company.account_id,
                buyer_account_id=account.account_id,
                shares=shares,
            )
            if result.ok:
                o = result.value
                p.send_message(
                    plugin.text("stock.bought", shares=o.shares,
                                price=format_amount(o.unit_price_minor),
                                total=format_amount(o.amount_minor),
                                holding=o.new_holding)
                )
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 卖出 ====================

    def open_sell(self, player, company) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        listing = service.listing_of(company.account_id)
        if listing is None:
            return
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return

        pos = service.my_position(account.account_id, company.account_id)
        company_cash = plugin.economy_service.get_balance(company.account_id).balance_minor
        price = max(1, listing.last_price_minor)
        # 公司现金能回购多少股 —— 赎回是公司掏钱，钱不够就只能少卖
        affordable = company_cash // price
        max_shares = min(pos["shares"], affordable)

        form = ModalForm(
            title=plugin.text("stock.sell.title", name=company.name),
            content=plugin.text(
                "stock.sell.hint",
                price=format_amount(price),
                held=pos["shares"],
                max=max_shares,
                cash=format_amount(company_cash),
            ),
        )
        form.add_control(_input(plugin.text("stock.sell.shares"), str(max(1, max_shares))))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                shares = int(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            if shares <= 0:
                p.send_message(plugin.text("err.invalid_amount"))
                return
            result = service.redeem(
                company_account_id=company.account_id,
                seller_account_id=account.account_id,
                shares=shares,
            )
            if result.ok:
                o = result.value
                p.send_message(
                    plugin.text("stock.sold", shares=o.shares,
                                price=format_amount(o.unit_price_minor),
                                total=format_amount(o.amount_minor),
                                holding=o.new_holding)
                )
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 股东列表 ====================

    def open_holders(self, player, company) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        holders = service.shareholders_of(company.account_id)
        listing = service.listing_of(company.account_id)
        total = listing.circulating_shares if listing else 0

        lines = [plugin.text("stock.holders.header", n=len(holders), total=total)]
        for holding in holders[:15]:
            share_bps = (holding.shares * 10000 // total) if total else 0
            lines.append(
                plugin.text(
                    "stock.holders.line",
                    name=holding.holder_name or holding.holder_account_id[:8],
                    shares=holding.shares,
                    pct=share_bps / 100,
                )
            )
        if not holders:
            lines.append(plugin.text("stock.holders.none"))

        form = ActionForm(title=plugin.text("stock.holders.title", name=company.name),
                          content="\n".join(lines))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_detail(p, company))
        player.send_form(form)

    # ==================== 我的持仓 ====================

    def open_portfolio(self, player) -> None:
        plugin = self.plugin
        service = plugin.stock_service
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return

        summary = service.portfolio_summary(account.account_id)
        color = _trend_color(summary["profit_minor"])
        lines = [
            plugin.text(
                "stock.portfolio.summary",
                value=format_amount(summary["market_value_minor"]),
                cost=format_amount(summary["cost_basis_minor"]),
                color=color,
                profit=format_amount(summary["profit_minor"]),
            ),
            "",
        ]
        if not summary["holdings"]:
            lines.append(plugin.text("stock.portfolio.empty"))
        for holding in summary["holdings"]:
            hcolor = _trend_color(holding.profit_minor)
            lines.append(
                plugin.text(
                    "stock.portfolio.line",
                    name=holding.company_name or holding.company_account_id[:8],
                    shares=holding.shares,
                    value=format_amount(holding.market_value_minor),
                    color=hcolor,
                    profit=format_amount(holding.profit_minor),
                )
            )

        form = ActionForm(title=plugin.text("stock.portfolio.title"), content="\n".join(lines))
        for holding in summary["holdings"][:10]:
            service_co = service.companies.repo.get_by_account(holding.company_account_id)
            if service_co is None:
                continue
            form.add_button(
                f"§e{service_co.name} §7×{holding.shares}",
                on_click=lambda p, c=service_co: self.open_detail(p, c),
            )
        form.add_button(plugin.text("stock.market.back"),
                        on_click=lambda p: self.open_market(p))
        player.send_form(form)
