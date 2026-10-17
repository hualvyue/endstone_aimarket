"""自选显示的 GUI。

玩家在这里勾选要在侧边栏/行动栏看到什么。勾选即生效，不需要重载任何东西。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from endstone.form import ActionForm

from .prefs import CARRIER_ACTIONBAR, CARRIER_NONE, CARRIER_SIDEBAR


class UIForms:
    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 主面板 ====================

    def open_main(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            player.send_message(plugin.text("err.account_not_found"))
            return

        ui = plugin.ui_service
        prefs = ui.get_prefs(account.account_id)

        content = [
            plugin.text("ui.prefs.header"),
            plugin.text("ui.prefs.carrier", value=ui.carrier_name(prefs.carrier)),
            plugin.text("ui.prefs.count", n=len(prefs.items)),
            "",
            plugin.text("ui.prefs.preview"),
            ui.preview(player, account.account_id),
        ]

        form = ActionForm(title=plugin.text("ui.prefs.title"), content="\n".join(content))

        # 显示位置切换
        for carrier in (CARRIER_SIDEBAR, CARRIER_ACTIONBAR, CARRIER_NONE):
            mark = "§a✓ " if prefs.carrier == carrier else "§7  "
            form.add_button(
                f"{mark}{plugin.text('ui.prefs.set_carrier', value=ui.carrier_name(carrier))}",
                on_click=lambda p, c=carrier: self._set_carrier(p, c),
            )

        form.add_button(plugin.text("ui.prefs.toggle_items"), on_click=lambda p: self.open_items(p))
        form.add_button(plugin.text("ui.prefs.add_stock"), on_click=lambda p: self.open_stock_picker(p))
        form.add_button(plugin.text("ui.prefs.clear"), on_click=lambda p: self._clear(p))
        form.add_button(plugin.text("ui.prefs.reset"), on_click=lambda p: self._reset(p))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 勾选项目 ====================

    def open_items(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        ui = plugin.ui_service
        prefs = ui.get_prefs(account.account_id)

        form = ActionForm(
            title=plugin.text("ui.items.title"),
            content=plugin.text("ui.items.hint", n=len(prefs.items)),
        )
        for item in ui.items_for_gui():
            on = item.key in prefs.items
            mark = "§a[✓]" if on else "§7[ ]"
            label = f"{mark} §f{item.name}"
            if item.description:
                label += f" §8{item.description}"
            form.add_button(label, on_click=lambda p, k=item.key: self._toggle(p, k))

        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main(p))
        player.send_form(form)

    # ==================== 加一支股票 ====================

    def open_stock_picker(self, player) -> None:
        plugin = self.plugin
        stock = getattr(plugin, "stock_service", None)
        if stock is None:
            player.send_message(plugin.text("ui.stock.none"))
            return

        listings = stock.all_listings()
        form = ActionForm(
            title=plugin.text("ui.stock.title"),
            content=plugin.text("ui.stock.hint", n=len(listings)),
        )
        if not listings:
            form.add_button(plugin.text("ui.stock.none"), on_click=lambda p: None)
        for listing in listings[:30]:
            company = None
            if stock.companies is not None:
                company = stock.companies.repo.get_by_account(listing.company_account_id)
            if company is None:
                continue
            label = plugin.text(
                "ui.stock.entry",
                name=company.name,
                price=stock.describe_listing(listing, company.name),
            )
            form.add_button(
                label,
                on_click=lambda p, n=company.name: self._add_stock(p, n),
            )

        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main(p))
        player.send_form(form)

    # ==================== 动作 ====================

    def _toggle(self, player, key: str) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        plugin.ui_service.toggle(account.account_id, key)
        plugin.ui_service.refresh_player(player)
        self.open_items(player)

    def _set_carrier(self, player, carrier: str) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        plugin.ui_service.set_carrier(account.account_id, carrier)
        plugin.ui_service.refresh_player(player)
        self.open_main(player)

    def _add_stock(self, player, company_name: str) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        plugin.ui_service.add_dynamic(account.account_id, f"stock:{company_name}")
        plugin.ui_service.refresh_player(player)
        player.send_message(plugin.text("ui.stock.added", name=company_name))
        self.open_items(player)

    def _clear(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        plugin.ui_service.clear(account.account_id)
        plugin.ui_service.refresh_player(player)
        player.send_message(plugin.text("ui.prefs.cleared"))
        self.open_main(player)

    def _reset(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        plugin.ui_service.reset_default(account.account_id)
        plugin.ui_service.refresh_player(player)
        player.send_message(plugin.text("ui.prefs.reset_done"))
        self.open_main(player)

    # ==================== 供命令使用 ====================

    def toggle_by_key(self, player, key: str) -> bool:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return False
        if not plugin.ui_service.registry.is_known(key):
            return False
        plugin.ui_service.toggle(account.account_id, key)
        plugin.ui_service.refresh_player(player)
        return True

    def list_keys(self) -> List[str]:
        return [i.key for i in self.plugin.ui_service.items_for_gui()]
