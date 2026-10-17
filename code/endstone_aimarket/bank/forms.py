"""央行界面的全部表单。

与商店界面同样的两条硬约束：
1. **不使用任何贴图/图标** —— 全部纯文字按钮。
2. 回调数据可能是 JSON 字符串也可能是 list，统一用 `_submitted()` 归一化。

界面分两层：**玩家层**（存贷汇兑）与**行长层**（货币政策、监管、公开市场操作）。
行长层的入口只有行长看得见。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from endstone.form import ActionForm, ModalForm, TextInput, Toggle

from ..core.money import format_amount, parse_amount


def _submitted(raw) -> List[Any]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return list(data) if isinstance(data, list) else [data]
        except Exception:
            return []
    return list(raw or [])


def _text_input(label: str, default: str = "", placeholder: str = "") -> TextInput:
    try:
        return TextInput(label=label, placeholder=placeholder, default_value=str(default))
    except TypeError:
        return TextInput(label=label, default_value=str(default))


class BankForms:
    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 工具 ====================

    def _account(self, player):
        return self.plugin.economy_api.api_ensure_player(player)

    def _money(self, minor: int) -> str:
        return format_amount(int(minor or 0))

    def _toast(self, player, result) -> bool:
        """把 Result 的成败播报给玩家。返回是否成功。"""
        if result is None:
            return False
        if result.ok:
            return True
        player.send_message(self.plugin.err_message(result))
        return False

    # ==================== 主菜单 ====================

    def open_home(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            player.send_message(plugin.text("err.account_not_found"))
            return

        bank = plugin.bank_service
        sheet = bank.balance_sheet()
        deposits = plugin.bank_deposit
        lending = plugin.bank_lending

        demand = deposits.total_of(account.account_id, kind="demand")
        fixed = deposits.total_of(account.account_id, kind="time")
        wealth = deposits.total_of(account.account_id, kind="wealth")
        overdraft = lending.overdraft_used(account.account_id)
        card_used = lending.card_used(account.account_id)
        mortgages = lending.mortgages_of(account.account_id)

        lines = [
            plugin.text("bank.home.balance", value=self._money(account.balance_minor)),
            plugin.text("bank.home.deposit_line",
                        demand=self._money(demand), fixed=self._money(fixed),
                        wealth=self._money(wealth)),
        ]
        if overdraft or card_used:
            lines.append(plugin.text("bank.home.debt_line",
                                     overdraft=self._money(overdraft),
                                     card=self._money(card_used)))
        if mortgages:
            total = sum(m.outstanding_minor for m in mortgages)
            lines.append(plugin.text("bank.home.mortgage_line",
                                     n=len(mortgages), value=self._money(total)))
        policy = bank.policy()
        lines.append(plugin.text("bank.home.rates",
                                 demand=policy.demand_rate_bps() / 100,
                                 time=policy.time_rate_bps() / 100,
                                 credit=policy.credit_rate_bps() / 100))
        lines.append(plugin.text("bank.home.supply",
                                 m0=self._money(sheet.m0_minor),
                                 m1=self._money(sheet.m1_minor)))

        form = ActionForm(title=plugin.text("bank.home.title"), content="\n".join(lines))
        form.add_button(plugin.text("bank.menu.deposit"), on_click=lambda p: self.open_deposit(p))
        form.add_button(plugin.text("bank.menu.time"), on_click=lambda p: self.open_time(p))
        form.add_button(plugin.text("bank.menu.overdraft"), on_click=lambda p: self.open_overdraft(p))
        form.add_button(plugin.text("bank.menu.card"), on_click=lambda p: self.open_card(p))
        form.add_button(plugin.text("bank.menu.mortgage"), on_click=lambda p: self.open_mortgage(p))
        form.add_button(plugin.text("bank.menu.wealth"), on_click=lambda p: self.open_wealth(p))
        if bank.is_governor_player(player):
            form.add_button(plugin.text("bank.menu.governor"), on_click=lambda p: self.open_governor(p))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 活期存款 ====================

    def open_deposit(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        deposits = plugin.bank_deposit
        demand = deposits.demand_of(account.account_id)
        rate = deposits.demand_rate_bps()

        lines = [
            plugin.text("bank.deposit.rate", rate=rate / 100),
            plugin.text("bank.deposit.balance",
                        value=self._money(demand.total_minor if demand else 0)),
            plugin.text("bank.wallet.balance", value=self._money(account.balance_minor)),
        ]
        if demand and demand.accrued_minor:
            lines.append(plugin.text("bank.deposit.interest",
                                     value=self._money(demand.accrued_minor)))

        form = ActionForm(title=plugin.text("bank.deposit.title"), content="\n".join(lines))
        form.add_button(plugin.text("bank.deposit.put"), on_click=lambda p: self._ask_deposit(p, "demand"))
        form.add_button(plugin.text("bank.deposit.take"), on_click=lambda p: self._ask_withdraw(p))
        form.add_button(plugin.text("bank.deposit.take_all"), on_click=lambda p: self._withdraw_all(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _ask_deposit(self, player, kind: str, days: int = 0) -> None:
        plugin = self.plugin
        title_key = "bank.deposit.put_title" if kind == "demand" else "bank.time.put_title"
        form = ModalForm(title=plugin.text(title_key))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "100"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            amount = parse_amount(str(values[0]))
            if amount <= 0:
                p.send_message(plugin.text("bank.bad_amount"))
                return
            account = self._account(p)
            if account is None:
                return
            result = plugin.bank_deposit.deposit(
                holder_account_id=account.account_id, amount_minor=amount, kind=kind, days=days
            )
            if result.ok:
                p.send_message(plugin.text("bank.deposit.done", value=self._money(amount)))
                self.open_deposit(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    def _ask_withdraw(self, player) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.deposit.take_title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "100"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            amount = parse_amount(str(values[0]))
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = plugin.bank_deposit.withdraw(
                holder_account_id=account.account_id, amount_minor=amount
            )
            if result.ok:
                p.send_message(plugin.text(
                    "bank.deposit.take_done", value=self._money(result.value["taken_minor"])
                ))
                self.open_deposit(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    def _withdraw_all(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        available = plugin.bank_deposit.available_of(account.account_id)
        if available <= 0:
            player.send_message(plugin.text("bank.nothing_to_take"))
            return
        result = plugin.bank_deposit.withdraw(
            holder_account_id=account.account_id, amount_minor=available
        )
        if result.ok:
            player.send_message(plugin.text(
                "bank.deposit.take_done", value=self._money(result.value["taken_minor"])
            ))
        else:
            player.send_message(plugin.err_message(result))
        self.open_deposit(player)

    # ==================== 定期 ====================

    def open_time(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        deposits = plugin.bank_deposit
        fixed = deposits.repo.deposits_of(account.account_id, kind="time")
        lines = [plugin.text("bank.time.rate", rate=deposits.time_rate_bps() / 100)]
        for item in fixed:
            lines.append("§7· " + item.describe())

        form = ActionForm(title=plugin.text("bank.time.title"), content="\n".join(lines))
        for days in (7, 30, 90, 180):
            form.add_button(
                plugin.text("bank.time.option", days=days),
                on_click=lambda p, d=days: self._ask_time_days(p, d),
            )
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _ask_time_days(self, player, days: int) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.time.put_title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "100"))
        form.add_control(_text_input(plugin.text("bank.field.days"), str(days)))

        def on_submit(p, raw):
            values = _submitted(raw)
            if len(values) < 2:
                return
            amount = parse_amount(str(values[0]))
            d = int(str(values[1]).strip() or days)
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = plugin.bank_deposit.deposit(
                holder_account_id=account.account_id, amount_minor=amount, kind="time", days=d
            )
            if result.ok:
                p.send_message(plugin.text("bank.time.done", value=self._money(amount), days=d))
                self.open_time(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 透支 ====================

    def open_overdraft(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        lending = plugin.bank_lending
        line = lending.credit_line_of(account.account_id)
        available = lending.overdraft_available(account.account_id)
        used = lending.overdraft_used(account.account_id)

        if line is None:
            content = plugin.text("bank.overdraft.none")
        else:
            content = "\n".join([
                plugin.text("bank.overdraft.limit", value=self._money(line.credit_limit_minor)),
                plugin.text("bank.overdraft.used", value=self._money(used)),
                plugin.text("bank.overdraft.available", value=self._money(available)),
                plugin.text("bank.overdraft.rate", rate=line.rate_bps_per_day / 100),
            ])

        form = ActionForm(title=plugin.text("bank.overdraft.title"), content=content)
        if line is None:
            form.add_button(plugin.text("bank.overdraft.open"), on_click=lambda p: self._open_overdraft(p))
        else:
            if available > 0:
                form.add_button(plugin.text("bank.overdraft.draw"), on_click=lambda p: self._ask_draw(p))
            if used > 0:
                form.add_button(plugin.text("bank.overdraft.repay"), on_click=lambda p: self._ask_repay(p, "overdraft"))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _open_overdraft(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        result = plugin.bank_lending.open_overdraft(account.account_id)
        if result.ok:
            player.send_message(plugin.text(
                "bank.overdraft.opened", value=self._money(result.value.credit_limit_minor)
            ))
        else:
            player.send_message(plugin.err_message(result))
        self.open_overdraft(player)

    def _ask_draw(self, player) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.overdraft.draw_title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "100"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            amount = parse_amount(str(values[0]))
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = plugin.bank_lending.draw_overdraft(account.account_id, amount)
            if result.ok:
                p.send_message(plugin.text("bank.overdraft.drawn", value=self._money(amount)))
                self.open_overdraft(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    def _ask_repay(self, player, product: str) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.repay.title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "100"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            amount = parse_amount(str(values[0]))
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = (
                plugin.bank_lending.repay_overdraft(account.account_id, amount)
                if product == "overdraft"
                else plugin.bank_lending.card_pay(account.account_id, amount)
            )
            if result.ok:
                p.send_message(plugin.text(
                    "bank.repay.done",
                    value=self._money(result.value["paid_minor"]),
                    left=self._money(result.value["remaining_minor"]),
                ))
                self.open_overdraft(p) if product == "overdraft" else self.open_card(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 信用卡 ====================

    def open_card(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        lending = plugin.bank_lending
        card = lending.card_of(account.account_id)

        if card is None:
            content = plugin.text("bank.card.none")
        else:
            used = lending.card_used(account.account_id)
            statements = plugin.bank_service.repo.statements_of(card.card_id)[:3]
            lines = [
                plugin.text("bank.card.limit", value=self._money(card.credit_limit_minor)),
                plugin.text("bank.card.used", value=self._money(used)),
                plugin.text("bank.card.available", value=self._money(lending.card_available(account.account_id))),
                plugin.text("bank.card.rate", rate=card.apr_bps_per_day / 100,
                            cycle=card.cycle_days, grace=card.grace_days),
            ]
            for stmt in statements:
                lines.append(
                    plugin.text("bank.card.statement",
                                amount=self._money(stmt.amount_minor),
                                min=self._money(stmt.min_pay_minor),
                                status=stmt.status)
                )
            content = "\n".join(lines)

        form = ActionForm(title=plugin.text("bank.card.title"), content=content)
        if card is None:
            form.add_button(plugin.text("bank.card.issue"), on_click=lambda p: self._issue_card(p))
        else:
            if lending.card_used(account.account_id) > 0:
                form.add_button(plugin.text("bank.card.pay"), on_click=lambda p: self._ask_repay(p, "card"))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _issue_card(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        result = plugin.bank_lending.issue_card(account.account_id)
        if result.ok:
            player.send_message(plugin.text(
                "bank.card.issued", value=self._money(result.value.credit_limit_minor)
            ))
        else:
            player.send_message(plugin.err_message(result))
        self.open_card(player)

    # ==================== 按揭 ====================

    def open_mortgage(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        lending = plugin.bank_lending
        items = lending.mortgages_of(account.account_id)
        shops = plugin.shop_repo.list_by_owner(account.account_id) \
            if hasattr(plugin.shop_repo, "list_by_owner") else []

        lines = [plugin.text("bank.mortgage.hint")]
        for m in items:
            lines.append("§7· " + m.describe())
        if not items:
            lines.append(plugin.text("bank.mortgage.none"))

        form = ActionForm(title=plugin.text("bank.mortgage.title"), content="\n".join(lines))
        for shop in shops[:6]:
            value = lending.estimate_shop(shop)
            quota = lending.mortgage_quota(shop)
            if quota <= 0:
                continue
            name = (shop.get("offer_item_json") or "")[:0] or str(shop.get("shop_id"))[:8]
            form.add_button(
                plugin.text("bank.mortgage.pick", shop=name, quota=self._money(quota)),
                on_click=lambda p, s=shop: self._ask_mortgage(p, s),
            )
        if items:
            form.add_button(plugin.text("bank.mortgage.repay"), on_click=lambda p: self._ask_repay(p, "mortgage"))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _ask_mortgage(self, player, shop: Dict[str, Any]) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.mortgage.apply_title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "1000"))
        form.add_control(_text_input(plugin.text("bank.field.periods"), "6"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if len(values) < 2:
                return
            amount = parse_amount(str(values[0]))
            periods = int(str(values[1]).strip() or 6)
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = plugin.bank_lending.open_mortgage(
                borrower_account_id=account.account_id, shop=shop,
                amount_minor=amount, periods=periods,
            )
            if result.ok:
                p.send_message(plugin.text("bank.mortgage.done", value=self._money(amount)))
                self.open_mortgage(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 理财（端口） ====================

    def open_wealth(self, player) -> None:
        plugin = self.plugin
        products = plugin.bank_wealth.list_products()
        lines = [plugin.text("bank.wealth.hint")]
        for item in products:
            lines.append("§7· " + plugin.bank_wealth.describe_product(item))
            # 策略接口：将来接 AI 后，这里显示的就可能是"由 AI 动态决定"的产品
            lines.append(plugin.text("bank.wealth.strategy",
                                     strategy=plugin.bank_wealth.strategy(
                                         str(item.get("strategy_key") or "fixed")).name))

        form = ActionForm(title=plugin.text("bank.wealth.title"), content="\n".join(lines))
        for item in products:
            form.add_button(
                plugin.text("bank.wealth.buy", name=item.get("name")),
                on_click=lambda p, pid=item.get("product_id"): self._ask_wealth(p, pid),
            )
        form.add_button(plugin.text("bank.wealth.mine"), on_click=lambda p: self.open_my_wealth(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _ask_wealth(self, player, product_id: str) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.wealth.buy_title"))
        form.add_control(_text_input(plugin.text("bank.field.amount"), "1000"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            amount = parse_amount(str(values[0]))
            account = self._account(p)
            if account is None or amount <= 0:
                return
            result = plugin.bank_wealth.subscribe(
                account_id=account.account_id, product_id=product_id, amount_minor=amount
            )
            if result.ok:
                p.send_message(plugin.text(
                    "bank.wealth.done",
                    value=self._money(amount),
                    rate=result.value["return_bps"] / 100,
                ))
                self.open_wealth(p)
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    def open_my_wealth(self, player) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        holdings = plugin.bank_wealth.holdings_of(account.account_id)
        lines = []
        for item in holdings:
            product = plugin.bank_wealth.get_product(item.get("product_id")) or {}
            lines.append(plugin.text(
                "bank.wealth.line",
                name=product.get("name", item.get("product_id")),
                value=self._money(item.get("amount_minor") or 0),
                rate=int(item.get("return_bps") or 0) / 100,
                status=item.get("status"),
            ))
        if not lines:
            lines.append(plugin.text("bank.wealth.empty"))

        form = ActionForm(title=plugin.text("bank.wealth.mine_title"), content="\n".join(lines))
        for item in holdings:
            if item.get("status") == "active":
                form.add_button(
                    plugin.text("bank.wealth.redeem", value=self._money(item.get("amount_minor") or 0)),
                    on_click=lambda p, hid=item.get("holding_id"): self._redeem_wealth(p, hid),
                )
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_wealth(p))
        player.send_form(form)

    def _redeem_wealth(self, player, holding_id: str) -> None:
        plugin = self.plugin
        account = self._account(player)
        if account is None:
            return
        result = plugin.bank_wealth.redeem(account_id=account.account_id, holding_id=holding_id)
        if result.ok:
            player.send_message(plugin.text(
                "bank.wealth.redeemed",
                value=self._money(result.value["paid_minor"]),
                profit=self._money(result.value["profit_minor"]),
            ))
        else:
            player.send_message(plugin.err_message(result))
        self.open_my_wealth(player)

    # ==================== 行长层 ====================

    def open_governor(self, player) -> None:
        plugin = self.plugin
        if not plugin.bank_service.is_governor_player(player):
            player.send_message(plugin.text("bank.not_governor"))
            return
        content = plugin.bank_policy.describe()
        form = ActionForm(title=plugin.text("bank.governor.title"), content=content)
        form.add_button(plugin.text("bank.governor.rate"), on_click=lambda p: self._set_rate(p))
        form.add_button(plugin.text("bank.governor.rrr"), on_click=lambda p: self._set_rrr(p))
        form.add_button(plugin.text("bank.governor.target"), on_click=lambda p: self._set_target(p))
        form.add_button(plugin.text("bank.governor.lock"), on_click=lambda p: self.open_locks(p))
        form.add_button(plugin.text("bank.governor.report"), on_click=lambda p: self.open_report(p))
        form.add_button(plugin.text("bank.governor.omo"), on_click=lambda p: self.open_omo(p))
        form.add_button(plugin.text("bank.governor.alerts"), on_click=lambda p: self.open_alerts(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_home(p))
        player.send_form(form)

    def _set_rate(self, player) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.governor.rate_title"))
        form.add_control(_text_input(plugin.text("bank.field.rate_bps"), "30"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            try:
                bps = int(str(values[0]).strip())
            except ValueError:
                p.send_message(plugin.text("bank.bad_amount"))
                return
            result = plugin.bank_service.set_base_rate(bps)
            if result.ok:
                p.send_message(plugin.text("bank.governor.rate_done", rate=bps / 100))
            else:
                p.send_message(plugin.err_message(result))
            self.open_governor(p)

        form.on_submit = on_submit
        player.send_form(form)

    def _set_rrr(self, player) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.governor.rrr_title"))
        form.add_control(_text_input(plugin.text("bank.field.rrr_bps"), "1000"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if not values:
                return
            try:
                bps = int(str(values[0]).strip())
            except ValueError:
                p.send_message(plugin.text("bank.bad_amount"))
                return
            result = plugin.bank_service.set_reserve_ratio(bps)
            if result.ok:
                p.send_message(plugin.text("bank.governor.rrr_done", rate=bps / 100))
            else:
                p.send_message(plugin.err_message(result))
            self.open_governor(p)

        form.on_submit = on_submit
        player.send_form(form)

    def _set_target(self, player) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text("bank.governor.target_title"))
        form.add_control(_text_input(plugin.text("bank.field.target_bps"), "200"))
        form.add_control(_text_input(plugin.text("bank.field.band_bps"), "100"))

        def on_submit(p, raw):
            values = _submitted(raw)
            if len(values) < 2:
                return
            try:
                target = int(str(values[0]).strip())
                band = int(str(values[1]).strip())
            except ValueError:
                p.send_message(plugin.text("bank.bad_amount"))
                return
            result = plugin.bank_service.set_inflation_target(target, band_bps=band)
            if result.ok:
                p.send_message(plugin.text("bank.governor.target_done", rate=target / 100, band=band / 100))
            else:
                p.send_message(plugin.err_message(result))
            self.open_governor(p)

        form.on_submit = on_submit
        player.send_form(form)

    def open_locks(self, player) -> None:
        """锁定/解锁政策项。锁定的项自动调节会跳过，相当于行长的"手动挡"。"""
        plugin = self.plugin
        policy = plugin.bank_service.policy()
        lines = [plugin.text("bank.lock.hint")]
        for key in ("base_rate", "rrr", "omo", "inflation_target"):
            mark = "§c已锁定" if policy.is_locked(key) else "§a自动"
            lines.append(f"§7· {key} §8{mark}")
        lines.append(plugin.text("bank.lock.auto_state",
                                 state="开启" if policy.auto_policy else "关闭"))

        form = ActionForm(title=plugin.text("bank.lock.title"), content="\n".join(lines))
        for key in ("base_rate", "rrr", "omo", "inflation_target"):
            form.add_button(
                plugin.text("bank.lock.toggle", key=key),
                on_click=lambda p, k=key: self._toggle_lock(p, k),
            )
        form.add_button(plugin.text("bank.lock.toggle_auto"), on_click=lambda p: self._toggle_auto(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_governor(p))
        player.send_form(form)

    def _toggle_lock(self, player, key: str) -> None:
        plugin = self.plugin
        policy = plugin.bank_service.policy()
        if policy.is_locked(key):
            plugin.bank_service.unlock_item(key)
            player.send_message(plugin.text("bank.lock.unlocked", key=key))
        else:
            plugin.bank_service.lock_item(key)
            player.send_message(plugin.text("bank.lock.locked", key=key))
        self.open_locks(player)

    def _toggle_auto(self, player) -> None:
        plugin = self.plugin
        policy = plugin.bank_service.policy()
        policy.auto_policy = not policy.auto_policy
        plugin.bank_service.save_policy(policy)
        player.send_message(plugin.text(
            "bank.lock.auto_done", state="开启" if policy.auto_policy else "关闭"
        ))
        self.open_locks(player)

    def open_report(self, player) -> None:
        plugin = self.plugin
        content = plugin.bank_regulator.report()
        form = ActionForm(title=plugin.text("bank.report.title"), content=content)
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_governor(p))
        player.send_form(form)

    def open_alerts(self, player) -> None:
        plugin = self.plugin
        content = plugin.bank_regulator.alerts_text()
        form = ActionForm(title=plugin.text("bank.alerts.title"), content=content)
        form.add_button(
            plugin.text("bank.alerts.clear"),
            on_click=lambda p: self._clear_alerts(p),
        )
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_governor(p))
        player.send_form(form)

    def _clear_alerts(self, player) -> None:
        plugin = self.plugin
        plugin.bank_service.repo.acknowledge_alerts()
        player.send_message(plugin.text("bank.alerts.cleared"))
        self.open_alerts(player)

    # ==================== 公开市场操作 ====================

    def open_omo(self, player) -> None:
        plugin = self.plugin
        reserves = plugin.bank_service.repo.list_reserves()
        lines = [plugin.text("bank.omo.hint")]
        lines.append(plugin.text(
            "bank.omo.headroom",
            value=self._money(plugin.bank_policy.omo_max_per_op_minor()),
            qe=self._money(plugin.bank_policy.qe_remaining_minor()),
        ))
        for reserve in reserves:
            lines.append("§7· " + reserve.describe())
        if not reserves:
            lines.append(plugin.text("bank.omo.no_reserve"))
        signal = plugin.bank_policy.fx_signal()
        if signal:
            lines.append(plugin.text("bank.omo.signal",
                                     reason=signal["reason"], suggest=signal["suggest"]))

        form = ActionForm(title=plugin.text("bank.omo.title"), content="\n".join(lines))
        form.add_button(plugin.text("bank.omo.buy"), on_click=lambda p: self._ask_omo(p, "buy"))
        form.add_button(plugin.text("bank.omo.sell"), on_click=lambda p: self._ask_omo(p, "sell"))
        form.add_button(plugin.text("bank.omo.history"), on_click=lambda p: self.open_omo_log(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_governor(p))
        player.send_form(form)

    def _ask_omo(self, player, side: str) -> None:
        plugin = self.plugin
        form = ModalForm(title=plugin.text(
            "bank.omo.buy_title" if side == "buy" else "bank.omo.sell_title"
        ))
        form.add_control(_text_input(plugin.text("bank.field.player"), player.name))
        form.add_control(_text_input(plugin.text("bank.field.item"), "minecraft:diamond"))
        form.add_control(_text_input(plugin.text("bank.field.qty"), "1"))
        form.add_control(Toggle(label=plugin.text("bank.omo.as_qe"), default_value=False))

        def on_submit(p, raw):
            values = _submitted(raw)
            if len(values) < 4:
                return
            target_name = str(values[0]).strip()
            item_key = str(values[1]).strip()
            try:
                qty = int(str(values[2]).strip())
            except ValueError:
                p.send_message(plugin.text("bank.bad_amount"))
                return
            is_qe = bool(values[3])
            target = plugin.server.get_player(target_name)
            if target is None:
                p.send_message(plugin.text("bank.omo.no_player", name=target_name))
                return
            account = plugin.economy_api.api_ensure_player(target)
            if account is None:
                return
            if side == "buy":
                result = plugin.bank_policy.omo_buy(
                    account_id=account.account_id, item_key=item_key, qty=qty,
                    player=target, is_qe=is_qe,
                )
            else:
                result = plugin.bank_policy.omo_sell(
                    account_id=account.account_id, item_key=item_key, qty=qty,
                    player=target, is_qe=is_qe,
                )
            if result.ok:
                p.send_message(plugin.text("bank.omo.done", detail=result.value.describe()
                                           if result.value else ""))
            else:
                p.send_message(plugin.err_message(result))
            self.open_omo(p)

        form.on_submit = on_submit
        player.send_form(form)

    def open_omo_log(self, player) -> None:
        plugin = self.plugin
        records = plugin.bank_service.repo.recent_omo(15)
        lines = [record.describe() for record in records] or [plugin.text("bank.omo.empty_log")]
        form = ActionForm(title=plugin.text("bank.omo.log_title"), content="\n".join(lines))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_omo(p))
        player.send_form(form)
