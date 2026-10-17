"""央行命令。

命令分两层权限：
  * `bank.use` —— 所有玩家（存取款、透支、信用卡、按揭、理财）
  * `bank.admin` —— 仅行长（货币政策、公开市场操作、监管、核销）

行长的判定在运行时再做一次（`is_governor_player`），权限节点只是第一道门。
"""
from __future__ import annotations

from typing import Any, List

from ..core.constants import OmoSide
from ..core.money import format_amount, parse_amount

BANK_COMMANDS = {
    "bank": {
        "description": "中央银行：存款、贷款、信用卡、按揭、货币政策",
        "usages": [
            "/bank",
            "/bank deposit <amount: float>",
            "/bank withdraw <amount: float>",
            "/bank time <amount: float> <days: int>",
            "/bank overdraft [amount: float]",
            "/bank repay <amount: float>",
            "/bank card",
            "/bank mortgage <amount: float> [periods: int]",
            "/bank wealth",
            "/bank report",
            "/bank rate <bps: int>",
            "/bank rrr <bps: int>",
            "/bank target <bps: int> [band: int]",
            "/bank omo <side: str> <player: str> <item: str> <qty: int>",
            "/bank qe <side: str> <player: str> <item: str> <qty: int> <confirm: str>",
            "/bank adjust <player: str> <amount: float>",
            "/bank writeoff <loan_id: str>",
            "/bank governors",
        ],
        "aliases": ["央行", "cb"],
        "permissions": ["bank.use"],
    },
}

BANK_PERMISSIONS = {
    "bank.use": {"description": "使用央行基础功能", "default": True},
    "bank.admin": {"description": "行使央行行长权力", "default": "op"},
}

# 给玩家看的简短帮助（避免 /bank 帮助太长）
PLAYER_HELP = [
    "/bank 直接打开面板即可，全部功能都在里面",
    "/bank deposit <金额> ｜ /bank withdraw <金额>",
    "/bank time <金额> <天数>",
    "/bank overdraft [金额] ｜ /bank repay <金额>",
]


class BankCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 入口 ====================

    def handle(self, sender, command, args: List[str]) -> bool:
        plugin = self.plugin
        if not plugin.bank_service.enabled():
            sender.send_message(plugin.text("bank.disabled"))
            return False

        sub = args[0].lower() if args else "gui"

        # 行长子命令需要先验权，并给出"为什么不行"的具体原因
        admin_subs = {"report", "rate", "rrr", "target", "omo", "qe", "adjust",
                      "writeoff", "governors", "lock", "unlock", "auto"}
        if sub in admin_subs and not self._require_governor(sender):
            return False

        handler = {
            "gui": lambda s, a: self._gui(s),
            "deposit": self._deposit,
            "withdraw": self._withdraw,
            "time": self._time,
            "overdraft": self._overdraft,
            "repay": self._repay,
            "card": self._card,
            "mortgage": self._mortgage,
            "wealth": self._wealth,
            "report": self._report,
            "rate": self._rate,
            "rrr": self._rrr,
            "target": self._target,
            "omo": self._omo,
            "qe": self._qe,
            "adjust": self._adjust,
            "writeoff": self._writeoff,
            "governors": self._governors,
            "lock": self._lock,
            "unlock": self._unlock,
            "auto": self._auto,
        }.get(sub)

        if handler is None:
            for line in PLAYER_HELP:
                sender.send_message("§7  " + line)
            return False
        return handler(sender, args[1:])

    # ==================== 权限 ====================

    def _require_governor(self, sender) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return True  # 控制台视同行长（与 OP 同权）
        if plugin.bank_service.is_governor_player(sender):
            return True
        hint = plugin.bank_service.governor_hint()
        sender.send_message(plugin.text("bank.not_governor"))
        if hint:
            sender.send_message("§7" + hint)
        return False

    # ==================== 玩家命令 ====================

    def _gui(self, sender) -> bool:
        if not self.plugin.is_player(sender):
            sender.send_message(self.plugin.text("err.not_player"))
            return False
        self.plugin.bank_forms.open_home(sender)
        return True

    def _account_id(self, sender):
        account = self.plugin.economy_api.api_ensure_player(sender)
        return account.account_id if account else None

    def _deposit(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        amount = self._amount(args[0] if args else "")
        account_id = self._account_id(sender)
        if amount is None or account_id is None:
            return False
        result = plugin.bank_deposit.deposit(holder_account_id=account_id, amount_minor=amount)
        sender.send_message(
            plugin.text("bank.deposit.done", value=format_amount(amount)) if result.ok
            else plugin.err_message(result)
        )
        return result.ok

    def _withdraw(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        amount = self._amount(args[0] if args else "")
        account_id = self._account_id(sender)
        if amount is None or account_id is None:
            return False
        result = plugin.bank_deposit.withdraw(holder_account_id=account_id, amount_minor=amount)
        sender.send_message(
            plugin.text("bank.deposit.take_done", value=format_amount(result.value["taken_minor"]))
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _time(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        if len(args) < 2:
            sender.send_message("§c用法：/bank time <金额> <天数>")
            return False
        amount = self._amount(args[0])
        try:
            days = int(args[1])
        except ValueError:
            sender.send_message("§c天数必须是整数")
            return False
        account_id = self._account_id(sender)
        if amount is None or account_id is None:
            return False
        result = plugin.bank_deposit.deposit(
            holder_account_id=account_id, amount_minor=amount, kind="time", days=days
        )
        sender.send_message(
            plugin.text("bank.time.done", value=format_amount(amount), days=days)
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _overdraft(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        account_id = self._account_id(sender)
        if account_id is None:
            return False
        if not args:
            result = plugin.bank_lending.open_overdraft(account_id)
            sender.send_message(
                plugin.text("bank.overdraft.opened",
                            value=format_amount(result.value.credit_limit_minor))
                if result.ok else plugin.err_message(result)
            )
            return result.ok
        amount = self._amount(args[0])
        if amount is None:
            return False
        result = plugin.bank_lending.draw_overdraft(account_id, amount)
        sender.send_message(
            plugin.text("bank.overdraft.drawn", value=format_amount(amount))
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _repay(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        amount = self._amount(args[0] if args else "")
        account_id = self._account_id(sender)
        if amount is None or account_id is None:
            return False
        result = plugin.bank_lending.repay_overdraft(account_id, amount)
        if not result.ok and result.code.value == "tx.not_found":
            result = plugin.bank_lending.card_pay(account_id, amount)
        sender.send_message(
            plugin.text("bank.repay.done",
                        value=format_amount(result.value["paid_minor"]),
                        left=format_amount(result.value["remaining_minor"]))
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _card(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        account_id = self._account_id(sender)
        if account_id is None:
            return False
        card = plugin.bank_lending.card_of(account_id)
        if card is None:
            result = plugin.bank_lending.issue_card(account_id)
            if not result.ok:
                sender.send_message(plugin.err_message(result))
                return False
            card = result.value
            sender.send_message(plugin.text(
                "bank.card.issued", value=format_amount(card.credit_limit_minor)
            ))
            return True
        sender.send_message("§6" + card.describe())
        return True

    def _mortgage(self, sender, args) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            return False
        account_id = self._account_id(sender)
        if account_id is None:
            return False
        if not args:
            items = plugin.bank_lending.mortgages_of(account_id)
            if not items:
                sender.send_message(plugin.text("bank.mortgage.none"))
                return False
            for m in items:
                sender.send_message("§7· " + m.describe())
            return True
        amount = self._amount(args[0])
        try:
            periods = int(args[1]) if len(args) > 1 else 6
        except ValueError:
            periods = 6
        if amount is None:
            return False
        shops = plugin.shop_repo.list_by_owner(account_id) if hasattr(
            plugin.shop_repo, "list_by_owner") else []
        if not shops:
            sender.send_message(plugin.text("bank.mortgage.no_shop"))
            return False
        # 默认拿估值最高的那家店作抵押
        best = max(shops, key=lambda s: plugin.bank_lending.estimate_shop(s))
        result = plugin.bank_lending.open_mortgage(
            borrower_account_id=account_id, shop=best, amount_minor=amount, periods=periods
        )
        sender.send_message(
            plugin.text("bank.mortgage.done", value=format_amount(amount))
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _wealth(self, sender, args) -> bool:
        plugin = self.plugin
        products = plugin.bank_wealth.list_products()
        sender.send_message(plugin.text("bank.wealth.title"))
        for item in products:
            sender.send_message("§7· " + plugin.bank_wealth.describe_product(item))
        if plugin.is_player(sender):
            plugin.bank_forms.open_wealth(sender)
        return True

    # ==================== 行长命令 ====================

    def _report(self, sender, args) -> bool:
        plugin = self.plugin
        if self.plugin.is_player(sender) and not args:
            plugin.bank_forms.open_report(sender)
            return True
        for line in plugin.bank_regulator.report().split("\n"):
            sender.send_message(line)
        return True

    def _rate(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            sender.send_message(plugin.text("bank.usage.rate"))
            return False
        try:
            bps = int(args[0])
        except ValueError:
            sender.send_message("§c基点必须是整数（30 = 0.30%/天）")
            return False
        result = plugin.bank_service.set_base_rate(bps)
        sender.send_message(
            plugin.text("bank.governor.rate_done", rate=bps / 100)
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _rrr(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            sender.send_message(plugin.text("bank.usage.rrr"))
            return False
        try:
            bps = int(args[0])
        except ValueError:
            return False
        result = plugin.bank_service.set_reserve_ratio(bps)
        sender.send_message(
            plugin.text("bank.governor.rrr_done", rate=bps / 100)
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _target(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            sender.send_message(plugin.text("bank.usage.target"))
            return False
        try:
            target = int(args[0])
            band = int(args[1]) if len(args) > 1 else None
        except ValueError:
            return False
        result = plugin.bank_service.set_inflation_target(target, band_bps=band)
        sender.send_message(
            plugin.text("bank.governor.target_done", rate=target / 100,
                        band=(band if band is not None else plugin.bank_service.policy().target_band_bps) / 100)
            if result.ok else plugin.err_message(result)
        )
        return result.ok

    def _omo(self, sender, args) -> bool:
        return self._market_op(sender, args, is_qe=False)

    def _qe(self, sender, args) -> bool:
        if not args or args[-1].lower() != "confirm":
            self.plugin.log.warning(
                f"{getattr(sender, 'name', '控制台')} 尝试执行量化操作，等待二次确认"
            )
            sender.send_message(self.plugin.text("bank.qe.need_confirm"))
            return False
        return self._market_op(sender, args[:-1], is_qe=True)

    def _market_op(self, sender, args, *, is_qe: bool) -> bool:
        plugin = self.plugin
        if len(args) < 4:
            sender.send_message(plugin.text("bank.usage.omo"))
            return False
        side = args[0].lower()
        if side not in (OmoSide.BUY.value, OmoSide.SELL.value):
            sender.send_message(plugin.text("bank.usage.omo"))
            return False
        target_name, item_key, qty_text = args[1], args[2], args[3]
        try:
            qty = int(qty_text)
        except ValueError:
            return False
        target = plugin.server.get_player(target_name)
        if target is None:
            sender.send_message(plugin.text("bank.omo.no_player", name=target_name))
            return False
        account = plugin.economy_api.api_ensure_player(target)
        if account is None:
            return False
        if is_qe:
            cooldown = plugin.bank_policy.qe_cooldown_ok()
            if not cooldown.ok:
                sender.send_message(plugin.err_message(cooldown))
                return False
        func = plugin.bank_policy.omo_buy if side == OmoSide.BUY.value else plugin.bank_policy.omo_sell
        result = func(account_id=account.account_id, item_key=item_key, qty=qty,
                      player=target, is_qe=is_qe)
        if result.ok:
            sender.send_message(plugin.text(
                "bank.omo.done",
                detail=result.value.describe() if result.value else "",
            ))
        else:
            sender.send_message(plugin.err_message(result))
        return result.ok

    def _adjust(self, sender, args) -> bool:
        plugin = self.plugin
        if len(args) < 2:
            sender.send_message(plugin.text("bank.usage.adjust"))
            return False
        target = plugin.server.get_player(args[0])
        if target is None:
            sender.send_message(plugin.text("bank.omo.no_player", name=args[0]))
            return False
        amount = self._amount(args[1])
        if amount is None:
            return False
        account = plugin.economy_api.api_ensure_player(target)
        if account is None:
            return False
        result = plugin.economy_service.admin_adjust(
            account.account_id, amount, reason=f"央行直投 by {getattr(sender, 'name', 'console')}"
        )
        if result.ok:
            sender.send_message(plugin.text("bank.adjust.done", value=format_amount(amount)))
        else:
            sender.send_message(plugin.err_message(result))
        return result.ok

    def _writeoff(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            sender.send_message(plugin.text("bank.usage.writeoff"))
            return False
        result = plugin.bank_regulator.write_off(args[0])
        if result.ok:
            sender.send_message(plugin.text(
                "bank.writeoff.done", value=format_amount(result.value["amount_minor"])
            ))
        else:
            sender.send_message(plugin.err_message(result))
        return result.ok

    def _governors(self, sender, args) -> bool:
        plugin = self.plugin
        names = plugin.bank_service.governor_names()
        bound = plugin.bank_service.list_governors()
        sender.send_message(plugin.text("bank.governors.title"))
        sender.send_message(plugin.text("bank.governors.config",
                                        names="、".join(names) if names else "（未配置）"))
        for gov in bound:
            sender.send_message(plugin.text(
                "bank.governors.line", name=gov.display_name or gov.xuid
            ))
        if not bound:
            sender.send_message("§7" + plugin.bank_service.governor_hint())
        return True

    def _lock(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            return False
        plugin.bank_service.lock_item(args[0])
        sender.send_message(plugin.text("bank.lock.locked", key=args[0]))
        return True

    def _unlock(self, sender, args) -> bool:
        plugin = self.plugin
        if not args:
            return False
        plugin.bank_service.unlock_item(args[0])
        sender.send_message(plugin.text("bank.lock.unlocked", key=args[0]))
        return True

    def _auto(self, sender, args) -> bool:
        plugin = self.plugin
        policy = plugin.bank_service.policy()
        policy.auto_policy = not policy.auto_policy
        plugin.bank_service.save_policy(policy)
        sender.send_message(plugin.text(
            "bank.lock.auto_done", state="开启" if policy.auto_policy else "关闭"
        ))
        return True

    # ==================== 工具 ====================

    def _amount(self, text: str):
        value = parse_amount(str(text or ""))
        if value <= 0:
            self.plugin.log.debug("金额解析失败或非正数")
            return None
        return value
