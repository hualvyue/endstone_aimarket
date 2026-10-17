"""拍卖命令：/auction。

玩家主路径走 GUI（/auction 直接打开市场面板），这里同时提供文本子命令，
方便控制台与喜欢敲命令的玩家：list / create / bid / mine / cancel。
"""
from __future__ import annotations

from typing import List

from ..core.ids import short_id
from ..core.money import format_amount, parse_amount

COMMANDS = {
    "auction": {
        "description": "拍卖行：现场拍 / 离线拍",
        "usages": [
            "/auction",
            "/auction list",
            "/auction create",
            "/auction bid <id: str> <amount: float>",
            "/auction mine",
            "/auction cancel <id: str>",
        ],
        "aliases": ["拍卖"],
        "permissions": ["aimarket.command.auction"],
    },
}

PERMISSIONS = {
    "aimarket.command.auction": {"description": "允许使用 /auction", "default": True},
}


class AuctionCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    def handle(self, sender, command, args: List[str]) -> bool:
        if command.name != "auction":
            return False
        if not self.plugin.is_player(sender):
            return self._handle_text(sender, args)
        if not args:
            self.plugin.auction_forms.open_market(sender)
            return True
        sub = args[0].lower()
        if sub in ("list", "市场", "列表"):
            return self._handle_text(sender, ["list"])
        if sub in ("create", "上架", "开拍"):
            self.plugin.auction_forms.open_create(sender)
            return True
        if sub in ("mine", "我的"):
            self.plugin.auction_forms.open_my(sender)
            return True
        if sub == "bid":
            return self._bid(sender, args)
        if sub == "cancel":
            return self._cancel(sender, args)
        self.plugin.auction_forms.open_market(sender)
        return True

    # ==================== 文本版（控制台 / 命令爱好者）====================

    def _handle_text(self, sender, args: List[str]) -> bool:
        plugin = self.plugin
        sub = args[0].lower() if args else "list"
        if sub not in ("list", "create", "bid", "mine", "cancel", "help"):
            sub = "list"
        if sub == "help":
            for line in (
                "/auction list              查看进行中的拍卖",
                "/auction create            GUI 上架（推荐）",
                "/auction bid <ID> <金额>  出价",
                "/auction mine              我的拍卖 / 我的出价",
                "/auction cancel <ID>       撤销（仅限无人出价）",
            ):
                sender.send_message(f"§7  {line}")
            return True

        if sub == "list":
            auctions = plugin.auction_service.list_open(limit=30)
            if not auctions:
                sender.send_message(plugin.text("auction.list_empty"))
                return True
            sender.send_message(plugin.text("auction.list_header", n=len(auctions)))
            for a in auctions:
                sender.send_message(
                    f"§e{a['auction_id'][:8]}§7 {plugin.auction_service.describe(a)}"
                )
            return True

        if sub == "mine":
            if not plugin.is_player(sender):
                sender.send_message(plugin.text("err.not_player"))
                return False
            account = plugin.economy_api.api_ensure_player(sender)
            if account is None:
                return False
            selling = plugin.auction_service.list_by_seller(account.account_id)
            bidding = plugin.auction_service.list_by_bidder(account.account_id)
            sender.send_message(plugin.text("auction.mine_selling", n=len(selling)))
            for a in selling:
                sender.send_message(f"§e{a['auction_id'][:8]}§7 {plugin.auction_service.describe(a)}")
            sender.send_message(plugin.text("auction.mine_bidding", n=len(bidding)))
            for a in bidding:
                sender.send_message(f"§e{a['auction_id'][:8]}§7 {plugin.auction_service.describe(a)}")
            return True

        if sub == "bid":
            return self._bid(sender, args)

        if sub == "cancel":
            return self._cancel(sender, args)

        sender.send_message(plugin.text("auction.help_hint"))
        return True

    def _bid(self, sender, args: List[str]) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        if len(args) < 3:
            sender.send_message(plugin.text("auction.usage_bid"))
            return False
        account = plugin.economy_api.api_ensure_player(sender)
        if account is None:
            return False
        auction = plugin.auction_service.get(args[1])
        if auction is None:
            sender.send_message(plugin.text("err.not_found", name="拍卖"))
            return False
        try:
            amount = parse_amount(args[2])
        except ValueError:
            sender.send_message(plugin.text("err.invalid_amount"))
            return False
        result = plugin.auction_service.place_bid(
            account_id=account.account_id, auction_id=auction["auction_id"], amount_minor=amount
        )
        if result.ok:
            a = result.value
            sender.send_message(
                plugin.text("auction.bid_ok",
                            price=format_amount(int(a["current_bid_minor"])))
            )
        else:
            sender.send_message(plugin.err_message(result))
        return True

    def _cancel(self, sender, args: List[str]) -> bool:
        plugin = self.plugin
        if not plugin.is_player(sender):
            sender.send_message(plugin.text("err.not_player"))
            return False
        if len(args) < 2:
            sender.send_message(plugin.text("auction.usage_cancel"))
            return False
        account = plugin.economy_api.api_ensure_player(sender)
        if account is None:
            return False
        result = plugin.auction_service.cancel_auction(
            account_id=account.account_id, auction_id=args[1]
        )
        if result.ok:
            sender.send_message(plugin.text("auction.cancelled"))
        else:
            sender.send_message(plugin.err_message(result))
        return True
