"""拍卖的 GUI 表单。与商店表单同样遵守两条硬性约束：

1. 不使用任何贴图/图标，全部纯文字按钮；
2. 表单回调统一归一化（JSON 字符串 / list 都兼容）。

背包快速填入：上架时直接从背包里选物品，潜影盒会标注「整箱」，数量默认填满背包内该物品数量。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from endstone.form import ActionForm, ModalForm, TextInput

from ..core.ids import short_id
from ..core.money import format_amount, parse_amount
from ..inventory import item_codec
from .models import AuctionKind


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


class AuctionForms:
    def __init__(self, plugin):
        self.plugin = plugin
        self._state: Dict[str, Dict[str, Any]] = {}

    # ==================== 市场列表 ====================

    def open_market(self, player) -> None:
        plugin = self.plugin
        auctions = plugin.auction_service.list_open(limit=15)
        form = ActionForm(
            title=plugin.text("auction.menu.title"),
            content=plugin.text("auction.menu.content"),
        )
        if not auctions:
            form.add_button(plugin.text("auction.list_empty"), on_click=lambda p: None)
        for a in auctions:
            label = "§e" + plugin.auction_service.describe(a)
            form.add_button(label, on_click=lambda p, x=a: self.open_detail(p, x))
        form.add_button(plugin.text("auction.menu.create"), on_click=lambda p: self.open_create(p))
        form.add_button(plugin.text("auction.menu.mine"), on_click=lambda p: self.open_my(p))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 拍卖详情 ====================

    def open_detail(self, player, auction: Dict[str, Any]) -> None:
        plugin = self.plugin
        item = _decode(auction.get("item_info_json"))
        name = item.get("name") or item.get("type") or "?"
        kind = "现场拍" if auction.get("kind") == AuctionKind.LIVE.value else "离线拍"
        account = plugin.economy_api.api_ensure_player(player)
        is_seller = account is not None and account.account_id == auction.get("seller_account_id")
        has_bid = int(auction.get("current_bid_minor", 0)) > 0

        lines = [
            plugin.text("auction.detail.item", name=f"{name} ×{int(auction.get('qty', 0))}"),
            plugin.text("auction.detail.kind", value=kind),
            plugin.text("auction.detail.reserve", value=format_amount(int(auction.get("reserve_minor", 0)))),
            plugin.text("auction.detail.current", value=format_amount(int(auction.get("current_bid_minor", 0)))),
            plugin.text("auction.detail.fee", value=format_amount(int(auction.get("fee_minor", 0)))),
            plugin.text("auction.detail.ends", value=_human_time(int(auction.get("ends_at_ms", 0)))),
            plugin.text("auction.detail.id", value=auction.get("auction_id", "")[:8]),
        ]
        form = ActionForm(title=f"§6{name}", content="\n".join(lines))

        if auction.get("status") == "open" and not is_seller:
            form.add_button(plugin.text("auction.detail.bid"), on_click=lambda p: self._open_bid(p, auction))
        if is_seller and auction.get("status") == "open" and not has_bid:
            form.add_button(plugin.text("auction.detail.cancel"), on_click=lambda p: self._confirm_cancel(p, auction))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_market(p))
        player.send_form(form)

    def _open_bid(self, player, auction: Dict[str, Any]) -> None:
        plugin = self.plugin
        reserve = int(auction.get("reserve_minor", 0))
        current = int(auction.get("current_bid_minor", 0))
        floor = max(reserve, current + 1)
        form = ModalForm(title=plugin.text("auction.bid.title"))
        form.add_control(_text_input(plugin.text("auction.bid.amount"), str(floor),
                                     plugin.text("auction.bid.amount_ph", floor=format_amount(floor))))
        form.add_control(_text_input(plugin.text("auction.bid.confirm"), "1", "1"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                amount = parse_amount(str(data[0]))
                confirm = str(data[1]).strip()
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            if confirm != "1":
                p.send_message(plugin.text("auction.bid.aborted"))
                return
            account = plugin.economy_api.api_ensure_player(p)
            if account is None:
                return
            result = plugin.auction_service.place_bid(
                account_id=account.account_id, auction_id=auction["auction_id"], amount_minor=amount
            )
            if result.ok:
                a = result.value
                p.send_message(plugin.text("auction.bid_ok", price=format_amount(int(a["current_bid_minor"]))))
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        player.send_form(form)

    def _confirm_cancel(self, player, auction: Dict[str, Any]) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        result = plugin.auction_service.cancel_auction(
            account_id=account.account_id, auction_id=auction["auction_id"]
        )
        if result.ok:
            player.send_message(plugin.text("auction.cancelled"))
        else:
            player.send_message(plugin.err_message(result))

    # ==================== 我的拍卖 ====================

    def open_my(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        selling = plugin.auction_service.list_by_seller(account.account_id, limit=15)
        bidding = plugin.auction_service.list_by_bidder(account.account_id, limit=15)
        form = ActionForm(
            title=plugin.text("auction.mine.title"),
            content=plugin.text("auction.mine.content", s=len(selling), b=len(bidding)),
        )
        for a in selling:
            label = "§6[售] " + plugin.auction_service.describe(a)
            form.add_button(label, on_click=lambda p, x=a: self.open_detail(p, x))
        for a in bidding:
            label = "§b[拍] " + plugin.auction_service.describe(a)
            form.add_button(label, on_click=lambda p, x=a: self.open_detail(p, x))
        if not selling and not bidding:
            form.add_button(plugin.text("auction.list_empty"), on_click=lambda p: None)
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_market(p))
        player.send_form(form)

    # ==================== 上架向导（背包快速填入）====================

    def open_create(self, player) -> None:
        plugin = self.plugin
        form = ActionForm(
            title=plugin.text("auction.create.title"),
            content=plugin.text("auction.create.choose_kind"),
        )
        form.add_button(plugin.text("auction.kind.offline"), on_click=lambda p: self._pick_item(p, AuctionKind.OFFLINE.value))
        form.add_button(plugin.text("auction.kind.live"), on_click=lambda p: self._pick_item(p, AuctionKind.LIVE.value))
        form.add_button(plugin.text("common.cancel"), on_click=lambda p: self.open_market(p))
        player.send_form(form)

    def _pick_item(self, player, kind: str) -> None:
        plugin = self.plugin
        self._state[player.name] = {"kind": kind}
        items = plugin.inventory.get_items(player, server=plugin.server)
        if not items:
            player.send_message(plugin.text("auction.create.empty_inventory"))
            return
        seen = {}
        for info in items:
            seen.setdefault(item_codec.match_key(info), info)
        unique = list(seen.values())

        form = ActionForm(
            title=plugin.text("auction.create.pick_title"),
            content=plugin.text("auction.create.pick_hint", count=len(unique)),
        )
        for info in unique[:40]:
            # 潜影盒（带 nbt_b64）标注"整箱"，提示会连同盒内物品一起上架
            suffix = " §7[整箱]" if info.get("nbt_b64") else ""
            label = f"{info.get('name')} ×{plugin.inventory.count_of_type(player, info.get('type'))}{suffix}"
            form.add_button(label, on_click=lambda p, i=info: self._after_pick(p, i))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_create(p))
        player.send_form(form)

    def _after_pick(self, player, item_info: Dict[str, Any]) -> None:
        plugin = self.plugin
        state = self._state.setdefault(player.name, {})
        state["item_info"] = item_info
        kind = state.get("kind", AuctionKind.OFFLINE.value)
        have = plugin.inventory.count(player, item_info)
        is_box = bool(item_info.get("nbt_b64"))

        form = ModalForm(title=plugin.text("auction.create.params_title"))
        form.add_control(_text_input(plugin.text("auction.create.qty"), str(have), plugin.text("auction.create.qty_ph")))
        form.add_control(_text_input(
            plugin.text("auction.create.reserve"),
            "0" if kind == AuctionKind.LIVE.value else "",
            plugin.text("auction.create.reserve_ph"),
        ))
        form.add_control(_text_input(plugin.text("auction.create.duration"), "", plugin.text("auction.create.duration_ph")))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                qty = max(1, int(str(data[0])))
                reserve = parse_amount(str(data[1])) if str(data[1]).strip() else 0
                dur_raw = str(data[2]).strip()
                duration = int(dur_raw) if dur_raw else None
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            account = plugin.economy_api.api_ensure_player(p)
            if account is None:
                return
            result = plugin.auction_service.create_auction(
                player=p, account_id=account.account_id, item_info=item_info,
                quantity=qty, kind=kind, reserve_minor=reserve, duration_seconds=duration,
            )
            if result.ok:
                a = result.value
                p.send_message(plugin.text(
                    "auction.created", id=a["auction_id"][:8],
                    fee=format_amount(int(a["fee_minor"])),
                    ends=_human_time(int(a["ends_at_ms"])),
                ))
            else:
                p.send_message(plugin.err_message(result))

        form.on_submit = on_submit
        if is_box:
            # 潜影盒提示：整箱上架
            player.send_message(plugin.text("auction.create.box_hint"))
        player.send_form(form)


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


def _human_time(ends_ms: int) -> str:
    from ..auction.service import now_ms
    left = max(0, int(ends_ms) - now_ms())
    if left <= 0:
        return "已结束"
    mins, secs = divmod(left // 1000, 60)
    if mins >= 60:
        hours, mins = divmod(mins, 60)
        return f"{hours}时{mins}分"
    if mins > 0:
        return f"{mins}分{secs}秒"
    return f"{secs}秒"
