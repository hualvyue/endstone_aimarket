"""商店界面的全部表单。

两条硬性约束：
1. **不使用任何贴图/图标**。原插件里的 `icon="textures/ui/..."` 引用的是客户端资源包，
   属于他人素材，这里一律去掉，全部改为纯文字按钮。
2. 表单回调在不同 Endstone 版本下可能是 JSON 字符串、也可能是 list，
   统一用 `_submitted()` 归一化，避免因签名差异整片界面失效。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from endstone.form import ActionForm, ModalForm, TextInput, Toggle

from ..core.constants import PricingMode, ShopStatus, ShopType, TradeDirection
from ..core.ids import short_id
from ..core.money import apply_bps, format_amount, parse_amount
from ..inventory import item_codec
from ..tx.models import TradeRequest

PAGE_SIZE = 12


def _submitted(raw) -> List[Any]:
    """把表单回调的数据统一成 list。"""
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


class ShopForms:
    def __init__(self, plugin):
        self.plugin = plugin
        # 每个玩家在建店向导里的临时状态
        self._build_state: Dict[str, Dict[str, Any]] = {}

    # ==================== 主菜单 ====================

    def open_main_menu(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        balance_text = format_amount(account.balance_minor) if account else format_amount(0)

        form = ActionForm(
            title=plugin.text("shop.menu.title"),
            content=plugin.text("shop.menu.content", balance=balance_text),
        )
        form.add_button(plugin.text("shop.menu.build"), on_click=lambda p: self.open_build_wizard(p))
        form.add_button(plugin.text("shop.menu.mine"), on_click=lambda p: self.open_my_shops(p))
        form.add_button(plugin.text("shop.menu.nearby"), on_click=lambda p: self.open_nearby(p))
        form.add_button(plugin.text("shop.menu.wallet"), on_click=lambda p: self.open_wallet(p))
        if plugin.is_admin(player):
            form.add_button(plugin.text("shop.menu.admin"), on_click=lambda p: self.open_admin(p))
            form.add_button(plugin.text("shop.menu.reload"), on_click=lambda p: self._reload(p))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 建店向导 ====================

    def open_build_wizard(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            player.send_message(plugin.text("err.account_not_found"))
            return
        limit = plugin.shop_service.max_shops_per_player()
        used = plugin.shop_repo.count_by_owner(account.account_id)
        if limit > 0 and used >= limit:
            player.send_message(plugin.text("shop.limit_reached", limit=limit))
            return

        self._build_state[player.name] = {"account_id": account.account_id}
        form = ActionForm(
            title=plugin.text("shop.build.title"),
            content=plugin.text("shop.build.choose_type"),
        )
        form.add_button(plugin.text("shop.type.sell"), on_click=lambda p: self._build_pick_item(p, ShopType.SELL.value))
        form.add_button(plugin.text("shop.type.buy"), on_click=lambda p: self._build_pick_item(p, ShopType.BUY.value))
        form.add_button(plugin.text("shop.type.barter"), on_click=lambda p: self._build_pick_item(p, ShopType.BARTER.value))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    def _build_pick_item(self, player, shop_type: str, *, for_barter_give: bool = False) -> None:
        """让玩家从背包里选一件物品。"""
        plugin = self.plugin
        state = self._build_state.setdefault(player.name, {})
        state["shop_type"] = shop_type

        items = plugin.inventory.get_items(player, server=plugin.server)
        if not items:
            player.send_message(plugin.text("shop.build.empty_inventory"))
            return

        # 同一个物品可能在多个槽位出现，这里按匹配键去重
        seen = {}
        for info in items:
            key = item_codec.match_key(info)
            if key not in seen:
                seen[key] = info
        unique = list(seen.values())

        field = "give_item" if for_barter_give else "offer_item"
        title = plugin.text("shop.build.pick_give") if for_barter_give else plugin.text("shop.build.pick_offer")

        form = ActionForm(title=title, content=plugin.text("shop.build.pick_hint", count=len(unique)))
        for info in unique[:40]:
            # 潜影盒（带 nbt_b64）标注「整箱」，提示会连同盒内物品一起上架
            suffix = " §7[整箱]" if info.get("nbt_b64") else ""
            label = f"{info.get('name')} ×{plugin.inventory.count_of_type(player, info.get('type'))}{suffix}"
            form.add_button(label, on_click=lambda p, i=info: self._build_after_pick(p, i, field))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_build_wizard(p))
        player.send_form(form)

    def _build_after_pick(self, player, item_info: Dict[str, Any], field: str) -> None:
        plugin = self.plugin
        state = self._build_state.setdefault(player.name, {})
        state[field] = item_info

        if field == "give_item":
            # 以物易物：选完给什么，再选要什么
            self._build_pick_item(player, ShopType.BARTER.value, for_barter_give=False)
            return
        if state.get("shop_type") == ShopType.BARTER.value:
            state["offer_item"] = item_info
            state["give_item"] = item_info
            self._build_barter_ratios(player)
            return
        self._build_set_price(player)

    # ---------- 以物易物比例 ----------

    def _build_barter_ratios(self, player) -> None:
        plugin = self.plugin
        state = self._build_state.get(player.name, {})
        give = state.get("give_item") or {}
        cost = state.get("offer_item") or {}

        form = ModalForm(title=plugin.text("shop.build.barter_title"))
        form.add_control(_text_input(plugin.text("shop.build.barter_cost_per_lot"), "1"))
        form.add_control(_text_input(plugin.text("shop.build.barter_give_per_lot"), "1"))
        form.add_control(_text_input(plugin.text("shop.build.stock"), "16"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                cost_per_lot = max(1, int(str(data[0])))
                give_per_lot = max(1, int(str(data[1])))
                stock = int(str(data[2]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            st = self._build_state.setdefault(p.name, {})
            st.update({"cost_per_lot": cost_per_lot, "give_per_lot": give_per_lot, "stock": stock})
            p.send_message(
                plugin.text(
                    "shop.build.barter_ready",
                    give=f"{give.get('name')}×{give_per_lot}",
                    cost=f"{cost.get('name')}×{cost_per_lot}",
                )
            )
            self._arm_build(p)

        form.on_submit = on_submit
        player.send_form(form)

    # ---------- 定价 ----------

    def _build_set_price(self, player) -> None:
        plugin = self.plugin
        state = self._build_state.get(player.name, {})
        item = state.get("offer_item") or {}
        shop_type = state.get("shop_type")
        item_key = item.get("type")
        official_ok = plugin.pricing.engine.has_rule(item_key)

        is_sell = shop_type == ShopType.SELL.value
        content = plugin.text("shop.build.price_hint")
        if official_ok:
            buy_p = plugin.pricing.engine.quote_price(item_key, TradeDirection.BUY.value)
            sell_p = plugin.pricing.engine.quote_price(item_key, TradeDirection.SELL.value)
            content += "\n" + plugin.text(
                "shop.build.official_ref",
                buy=format_amount(buy_p) if buy_p else "-",
                sell=format_amount(sell_p) if sell_p else "-",
            )

        form = ActionForm(title=plugin.text("shop.build.price_title"), content=content)
        if official_ok:
            form.add_button(
                plugin.text("shop.build.use_official"),
                on_click=lambda p: self._build_finish(p, PricingMode.OFFICIAL.value, None, 0),
            )
        form.add_button(
            plugin.text("shop.build.use_manual"),
            on_click=lambda p: self._build_manual_price(p),
        )
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_build_wizard(p))
        player.send_form(form)

    def _build_manual_price(self, player) -> None:
        plugin = self.plugin
        state = self._build_state.get(player.name, {})
        shop_type = state.get("shop_type")
        is_sell = shop_type == ShopType.SELL.value
        item = state.get("offer_item") or {}

        form = ModalForm(title=plugin.text("shop.build.manual_title"))
        form.add_control(_text_input(plugin.text("shop.build.unit_price"), "10", plugin.text("shop.build.unit_price_ph")))
        if is_sell:
            # 默认数量直接填满背包里该物品的数量，方便"整箱 / 全部上架"
            have = plugin.inventory.count(player, item) if item else 16
            form.add_control(_text_input(plugin.text("shop.build.stock"), str(max(1, have))))
        else:
            form.add_control(_text_input(plugin.text("shop.build.budget"), "100"))
        form.add_control(_text_input(plugin.text("shop.build.discount_bps"), "0", "0"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                unit = parse_amount(str(data[0]))
                second = parse_amount(str(data[1]))
                discount = int(str(data[2] or 0))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            if unit <= 0:
                p.send_message(plugin.text("err.invalid_amount"))
                return
            st = self._build_state.setdefault(p.name, {})
            if is_sell:
                st["stock"] = max(1, int(second))
                st["budget"] = 0
            else:
                st["budget"] = max(1, int(second))
                st["stock"] = 0
            self._build_finish(p, PricingMode.MANUAL.value, unit, discount)

        form.on_submit = on_submit
        player.send_form(form)

    def _build_finish(self, player, pricing_mode: str, unit_minor: Optional[int], discount_bps: int) -> None:
        """把向导结果挂到玩家身上，等他右键一个按钮来落点。"""
        plugin = self.plugin
        state = self._build_state.setdefault(player.name, {})
        state.update({
            "pricing_mode": pricing_mode,
            "unit_minor": unit_minor,
            "discount_bps": discount_bps,
        })
        plugin.arm_build(player, state)
        player.send_message(plugin.text("shop.build.armed"))

    def start_build_at(self, player, block) -> None:
        """玩家已经把店配置好，现在右键了一个按钮 —— 正式创建。"""
        plugin = self.plugin
        state = plugin.take_build_state(player)
        if not state:
            return
        account_id = state.get("account_id")
        item = state.get("offer_item") or {}
        shop_type = state.get("shop_type")
        dim = block.dimension.name
        x, y, z = block.x, block.y, block.z

        try:
            if shop_type == ShopType.SELL.value:
                result = plugin.shop_service.create_sell_shop(
                    player=player, account_id=account_id, dimension=dim, x=x, y=y, z=z,
                    item_info=item,
                    quantity=int(state.get("stock") or 1),
                    pricing_mode=state.get("pricing_mode", PricingMode.MANUAL.value),
                    unit_minor=state.get("unit_minor"),
                    discount_bps=int(state.get("discount_bps") or 0),
                )
            elif shop_type == ShopType.BUY.value:
                result = plugin.shop_service.create_buy_shop(
                    player=player, account_id=account_id, dimension=dim, x=x, y=y, z=z,
                    item_info=item,
                    budget_minor=int(state.get("budget") or 0),
                    pricing_mode=state.get("pricing_mode", PricingMode.MANUAL.value),
                    unit_minor=state.get("unit_minor"),
                    discount_bps=int(state.get("discount_bps") or 0),
                )
            else:
                result = plugin.shop_service.create_barter_shop(
                    player=player, account_id=account_id, dimension=dim, x=x, y=y, z=z,
                    give_item=state.get("give_item") or item,
                    cost_item=state.get("cost_item") or state.get("offer_item") or {},
                    give_per_lot=int(state.get("give_per_lot") or 1),
                    cost_per_lot=int(state.get("cost_per_lot") or 1),
                    quantity=int(state.get("stock") or 1),
                )
        except Exception as e:
            plugin.log.error(f"创建商店异常: {e}")
            player.send_message(plugin.text("err.internal"))
            return

        if result.ok:
            player.send_message(plugin.text("shop.build.created"))
        else:
            player.send_message(plugin.err_message(result))

    # ==================== 商店详情 ====================

    def open_shop_detail(self, player, shop: Dict[str, Any]) -> None:
        plugin = self.plugin
        info = _decode(shop.get("offer_item_json"))
        shop_type = shop.get("shop_type")
        lines = [plugin.text("shop.detail.shop_type", value=plugin.shop_type_name(shop_type))]

        if shop_type == ShopType.BARTER.value:
            cost = _decode(shop.get("cost_item_json"))
            lines.append(
                plugin.text(
                    "shop.detail.barter_rate",
                    give=info.get("name", "?"),
                    give_n=shop.get("offer_per_lot", 1),
                    cost=cost.get("name", "?"),
                    cost_n=shop.get("cost_per_lot", 1),
                )
            )
            lines.append(plugin.text("shop.detail.stock", value=int(shop.get("stock_qty", 0))))
        else:
            if int(shop.get("is_infinite", 0)):
                lines.append(plugin.text("shop.detail.infinite"))
            else:
                lines.append(plugin.text("shop.detail.stock", value=int(shop.get("stock_qty", 0))))
            buy_price = plugin.pricing.unit_minor(shop, TradeDirection.BUY.value)
            sell_price = plugin.pricing.unit_minor(shop, TradeDirection.SELL.value)
            if shop_type in (ShopType.SELL.value, ShopType.BOTH.value) and buy_price:
                lines.append(plugin.text("shop.detail.buy_price", value=format_amount(buy_price)))
            if shop_type in (ShopType.BUY.value, ShopType.BOTH.value) and sell_price:
                lines.append(plugin.text("shop.detail.sell_price", value=format_amount(sell_price)))
            if shop.get("pricing_mode") == PricingMode.OFFICIAL.value:
                lines.append(plugin.text("shop.detail.official_mode"))
                if shop_type == ShopType.BUY.value:
                    budget = plugin.escrow_balance(shop)
                    lines.append(plugin.text("shop.detail.budget", value=format_amount(budget)))
        lines.append(plugin.text("shop.detail.owner", value=plugin.owner_display(shop)))

        # 赞助商署名 —— 这就是"赞助"换来的广告位
        sponsor = plugin.sponsor_of(shop)
        if sponsor:
            lines.append(plugin.text("shop.detail.sponsor", name=sponsor))

        form = ActionForm(title=info.get("name") or plugin.text("shop.detail.title"), content="\n".join(lines))

        if shop_type == ShopType.BARTER.value:
            form.add_button(plugin.text("shop.detail.do_barter"), on_click=lambda p: self._barter_panel(p, shop))
        else:
            if plugin.pricing.can_buy(shop) and int(shop.get("is_infinite", 0) or shop.get("stock_qty", 0)) != 0:
                form.add_button(plugin.text("shop.detail.do_buy"), on_click=lambda p: self.open_trade_panel(p, shop, "buy"))
            elif shop_type in (ShopType.SELL.value, ShopType.BOTH.value):
                form.add_button(plugin.text("shop.detail.sold_out"), on_click=lambda p: None)

            if plugin.pricing.can_sell(shop) and shop_type in (ShopType.BUY.value, ShopType.BOTH.value):
                form.add_button(plugin.text("shop.detail.do_sell"), on_click=lambda p: self.open_trade_panel(p, shop, "sell"))

        if plugin.can_manage(player, shop):
            form.add_button(plugin.text("shop.detail.manage"), on_click=lambda p: self.open_manage_panel(p, shop))
        form.add_button(plugin.text("common.close"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 交易面板 ====================

    def open_trade_panel(self, player, shop: Dict[str, Any], direction: str) -> None:
        plugin = self.plugin
        info = _decode(shop.get("offer_item_json"))
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            player.send_message(plugin.text("err.account_not_found"))
            return

        quote_result = plugin.pricing.quote(shop, direction)
        if not quote_result.ok or quote_result.value.suspended:
            player.send_message(plugin.text("shop.price_unavailable"))
            return
        quote = quote_result.value
        unit = int(quote.unit_minor)

        capacity = plugin.inventory.capacity_for(player, info) if direction == "buy" else plugin.inventory.count(player, info)
        lines = [
            plugin.text("shop.trade.item", name=info.get("name", "?")),
            plugin.text("shop.trade.unit", value=format_amount(unit)),
            plugin.text("shop.trade.max", value=max(0, capacity)),
            plugin.text("shop.trade.tax", value=f"{plugin.shop_service.tax_bps() / 100:.2f}%"),
            "",
            plugin.text("shop.trade.detail", value=quote.describe()),
        ]
        if direction == "sell":
            lines.append(plugin.text("shop.trade.escrow", value=format_amount(plugin.escrow_balance(shop))))

        form = ModalForm(title=plugin.text("shop.trade.confirm_title"))
        form.add_control(_text_input(plugin.text("shop.trade.quantity"), "1", "1"))
        nonce = short_id(16)

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                quantity = int(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            if quantity <= 0:
                p.send_message(plugin.text("err.invalid_amount"))
                return
            self._do_trade(p, shop, direction, quantity, unit, quote.price_version)

        form.on_submit = on_submit
        player.send_form(form)

    def _do_trade(self, player, shop, direction, quantity, unit, price_version) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            player.send_message(plugin.text("err.account_not_found"))
            return

        # 幂等键把 玩家+商店+方向+数量+报价版本 绑在一起：
        # 同一个数量连点两次只会成交一次；改了数量就是新的一次交易。
        idem = f"{account.account_id}:{shop['shop_id']}:{direction}:{quantity}:{price_version}:{short_id(8)}"
        request = TradeRequest(
            player=player,
            shop=plugin.shop_repo.get(shop["shop_id"]) or shop,
            actor_account_id=account.account_id,
            direction=direction,
            quantity=quantity,
            idempotency_key=idem,
            unit_minor=unit,
            price_version=price_version,
            # 按店主的主体类型取税率：个人一个税率，公司走更高的营业额税率
            tax_bps=plugin.shop_service.tax_bps_for(shop.get("owner_account_id")),
        )
        outcome = plugin.coordinator.execute(request)
        if outcome.ok:
            if direction == "buy":
                player.send_message(
                    plugin.text(
                        "shop.trade.bought",
                        qty=outcome.filled_qty,
                        name=outcome.item_name,
                        cost=format_amount(outcome.paid_minor),
                        tax=format_amount(outcome.tax_minor),
                    )
                )
            else:
                player.send_message(
                    plugin.text(
                        "shop.trade.sold",
                        qty=outcome.filled_qty,
                        name=outcome.item_name,
                        income=format_amount(outcome.received_minor),
                        tax=format_amount(outcome.tax_minor),
                    )
                )
            if outcome.is_partial:
                player.send_message(plugin.text("shop.trade.partial", filled=outcome.filled_qty, want=outcome.requested_qty))
        else:
            player.send_message(plugin.error_text(outcome.code, outcome.message))

    def _barter_panel(self, player, shop) -> None:
        plugin = self.plugin
        give = _decode(shop.get("offer_item_json"))
        cost = _decode(shop.get("cost_item_json"))
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return

        stock = int(shop.get("stock_qty", 0))
        have = plugin.inventory.count(player, cost)
        per_lot_cost = max(1, int(shop.get("cost_per_lot", 1)))
        per_lot_give = max(1, int(shop.get("offer_per_lot", 1)))
        max_lots = min(stock // per_lot_give, have // per_lot_cost)

        form = ModalForm(
            title=plugin.text(
                "shop.barter.title", give=give.get("name", "?"), cost=cost.get("name", "?")
            )
        )
        form.add_control(_text_input(plugin.text("shop.barter.lots", max=max_lots), "1"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                lots = int(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            request = TradeRequest(
                player=p,
                shop=plugin.shop_repo.get(shop["shop_id"]) or shop,
                actor_account_id=account.account_id,
                direction="barter",
                quantity=lots,
                idempotency_key=f"{account.account_id}:{shop['shop_id']}:barter:{lots}:{short_id(8)}",
            )
            outcome = plugin.coordinator.execute_barter(request)
            if outcome.ok:
                p.send_message(
                    plugin.text(
                        "shop.barter.done",
                        cost=cost.get("name", "?"),
                        cost_n=lots * per_lot_cost,
                        give=give.get("name", "?"),
                        give_n=lots * per_lot_give,
                    )
                )
            else:
                p.send_message(plugin.error_text(outcome.code, outcome.message))

        form.on_submit = on_submit
        player.send_form(form)

    # ==================== 我的商店 ====================

    def open_my_shops(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        shops = plugin.shop_service.my_shops(account.account_id)

        form = ActionForm(
            title=plugin.text("shop.mine.title"),
            content=plugin.text("shop.mine.count", n=len(shops), limit=plugin.shop_service.max_shops_per_player()),
        )
        if not shops:
            form.add_button(plugin.text("shop.mine.empty"), on_click=lambda p: None)
        for shop in shops[:20]:
            info = _decode(shop.get("offer_item_json"))
            label = plugin.text(
                "shop.mine.entry",
                name=info.get("name", "?"),
                type=plugin.shop_type_name(shop.get("shop_type")),
                x=shop["x"], y=shop["y"], z=shop["z"],
                stock=int(shop.get("stock_qty", 0)),
            )
            form.add_button(label, on_click=lambda p, s=shop: self.open_manage_panel(p, s))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    def open_manage_panel(self, player, shop: Dict[str, Any]) -> None:
        plugin = self.plugin
        fresh = plugin.shop_repo.get(shop["shop_id"]) or shop
        info = _decode(fresh.get("offer_item_json"))
        shop_type = fresh.get("shop_type")
        lines = [
            plugin.text("shop.manage.type", value=plugin.shop_type_name(shop_type)),
            plugin.text("shop.manage.stock", value=int(fresh.get("stock_qty", 0))),
        ]
        collected = plugin.shop_repo.collected_of(fresh)
        if collected:
            total = sum(int(c.get("count", 0) or 0) for c in collected)
            lines.append(plugin.text("shop.manage.collected", n=len(collected), total=total))
        if shop_type == ShopType.BUY.value:
            lines.append(plugin.text("shop.manage.budget", value=format_amount(plugin.escrow_balance(fresh))))

        form = ActionForm(title=info.get("name") or plugin.text("shop.manage.title"), content="\n".join(lines))

        if shop_type == ShopType.SELL.value:
            form.add_button(plugin.text("shop.manage.restock"), on_click=lambda p: self._restock_panel(p, fresh))
        if shop_type == ShopType.BUY.value:
            form.add_button(plugin.text("shop.manage.inject"), on_click=lambda p: self._inject_panel(p, fresh))
        if collected:
            form.add_button(plugin.text("shop.manage.collect"), on_click=lambda p: self._collect(p, fresh))
        form.add_button(plugin.text("shop.manage.close_shop"), on_click=lambda p: self._confirm_close(p, fresh))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_my_shops(p))
        player.send_form(form)

    def _restock_panel(self, player, shop) -> None:
        plugin = self.plugin
        info = _decode(shop.get("offer_item_json"))
        have = plugin.inventory.count(player, info)
        form = ModalForm(title=plugin.text("shop.restock.title", name=info.get("name", "?")))
        form.add_control(_text_input(plugin.text("shop.restock.quantity", max=have), "1"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                qty = int(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            result = plugin.shop_service.restock_sell_shop(player=p, shop=shop, quantity=qty)
            p.send_message(
                plugin.text("shop.restock.done", n=result.value)
                if result.ok
                else plugin.err_message(result)
            )

        form.on_submit = on_submit
        player.send_form(form)

    def _inject_panel(self, player, shop) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        form = ModalForm(title=plugin.text("shop.inject.title"))
        form.add_control(_text_input(plugin.text("shop.inject.amount"), "100"))

        def on_submit(p, raw):
            data = _submitted(raw)
            try:
                amount = parse_amount(str(data[0]))
            except (ValueError, IndexError, TypeError):
                p.send_message(plugin.text("err.invalid_amount"))
                return
            result = plugin.shop_service.inject_budget(
                account_id=account.account_id, shop=shop, amount_minor=amount
            )
            p.send_message(
                plugin.text("shop.inject.done", value=format_amount(amount))
                if result.ok
                else plugin.err_message(result)
            )

        form.on_submit = on_submit
        player.send_form(form)

    def _collect(self, player, shop) -> None:
        plugin = self.plugin
        result = plugin.shop_service.collect_items(player=player, shop=shop)
        if result.ok:
            player.send_message(plugin.text("shop.collect.done", n=result.value.get("taken", 0)))
        else:
            player.send_message(plugin.err_message(result))

    def _confirm_close(self, player, shop) -> None:
        plugin = self.plugin
        form = ActionForm(
            title=plugin.text("shop.close.title"),
            content=plugin.text("shop.close.hint"),
        )
        form.add_button(plugin.text("shop.close.yes"), on_click=lambda p: self._do_close(p, shop))
        form.add_button(plugin.text("common.cancel"), on_click=lambda p: self.open_manage_panel(p, shop))
        player.send_form(form)

    def _do_close(self, player, shop) -> None:
        plugin = self.plugin
        result = plugin.shop_service.close_shop(player=player, shop=shop)
        if result.ok:
            player.send_message(
                plugin.text(
                    "shop.close.done",
                    items=result.value.get("returned_items", 0),
                    money=format_amount(result.value.get("returned_money", 0)),
                )
            )
        else:
            player.send_message(plugin.err_message(result))

    def confirm_delete_shop(self, player, shop) -> None:
        """左键点自己店的按钮 → 确认拆除。"""
        plugin = self.plugin
        info = _decode(shop.get("offer_item_json"))
        form = ActionForm(
            title=plugin.text("shop.remove.title"),
            content=plugin.text("shop.remove.hint", name=info.get("name", "?")),
        )
        form.add_button(plugin.text("shop.remove.yes"), on_click=lambda p: self._do_close(p, shop))
        form.add_button(plugin.text("common.cancel"), on_click=lambda p: None)
        player.send_form(form)

    # ==================== 附近 / 全部 ====================

    def open_nearby(self, player) -> None:
        plugin = self.plugin
        loc = getattr(player, "location", None)
        if loc is None:
            player.send_message(plugin.text("err.not_player"))
            return
        shops = plugin.shop_service.nearby(int(loc.x), int(loc.z), player.level.name, 1)
        form = ActionForm(
            title=plugin.text("shop.nearby.title"),
            content=plugin.text("shop.nearby.count", n=len(shops)),
        )
        for shop in shops[:20]:
            info = _decode(shop.get("offer_item_json"))
            label = plugin.text(
                "shop.nearby.entry",
                name=info.get("name", "?"),
                x=shop["x"], y=shop["y"], z=shop["z"],
            )
            form.add_button(label, on_click=lambda p, s=shop: self.open_shop_detail(p, s))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    def open_admin(self, player) -> None:
        plugin = self.plugin
        stats = plugin.shop_repo.stats()
        eco = plugin.economy_service
        lines = [
            plugin.text("admin.stats_shops", total=stats["total"], active=stats["active"], infinite=stats["infinite"]),
            plugin.text("admin.stats_money", value=format_amount(eco.total_supply())),
            plugin.text("admin.stats_minted", value=format_amount(eco.minted_total())),
            plugin.text("admin.stats_treasury", value=format_amount(eco.treasury_balance())),
            plugin.text("admin.stats_players", n=len(eco.repo.list_player_accounts())),
        ]
        form = ActionForm(title=plugin.text("admin.title"), content="\n".join(lines))
        form.add_button(plugin.text("admin.all_shops"), on_click=lambda p: self.open_all_shops(p, 0))
        form.add_button(plugin.text("admin.audit"), on_click=lambda p: self._show_audit(p))
        form.add_button(plugin.text("admin.reset_prices"), on_click=lambda p: self._reset_prices(p))
        form.add_button(plugin.text("admin.daily"), on_click=lambda p: self._daily(p))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    def open_all_shops(self, player, page: int) -> None:
        plugin = self.plugin
        shops = plugin.shop_service.all_shops(limit=500)
        start = max(0, page) * PAGE_SIZE
        chunk = shops[start:start + PAGE_SIZE]
        form = ActionForm(
            title=plugin.text("admin.all_shops_title"),
            content=plugin.text("admin.all_shops_count", n=len(shops), page=page + 1),
        )
        for shop in chunk:
            info = _decode(shop.get("offer_item_json"))
            label = plugin.text(
                "admin.shop_entry",
                name=info.get("name", "?"),
                owner=plugin.owner_display(shop),
                x=shop["x"], y=shop["y"], z=shop["z"],
            )
            form.add_button(label, on_click=lambda p, s=shop: self.open_shop_detail(p, s))
        if start + PAGE_SIZE < len(shops):
            form.add_button(plugin.text("admin.next_page"), on_click=lambda p: self.open_all_shops(p, page + 1))
        if page > 0:
            form.add_button(plugin.text("admin.prev_page"), on_click=lambda p: self.open_all_shops(p, page - 1))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_admin(p))
        player.send_form(form)

    # ==================== 钱包 / 审计 ====================

    def open_wallet(self, player) -> None:
        plugin = self.plugin
        account = plugin.economy_api.api_ensure_player(player)
        if account is None:
            return
        balance = plugin.economy_service.get_balance(account.account_id)
        lines = [
            plugin.text("wallet.balance", value=format_amount(balance.balance_minor)),
            plugin.text("wallet.available", value=format_amount(balance.available_minor)),
        ]
        if balance.held_minor:
            lines.append(plugin.text("wallet.held", value=format_amount(balance.held_minor)))
        lines.append("")

        records = plugin.economy_service.history(account.account_id, limit=10)
        if not records:
            lines.append(plugin.text("wallet.no_history"))
        for record in records:
            from datetime import datetime

            when = datetime.fromtimestamp(record.created_at_ms / 1000).strftime("%m-%d %H:%M")
            sign = "+" if record.delta_minor >= 0 else "-"
            lines.append(
                plugin.text(
                    "wallet.history_line",
                    time=when,
                    kind=plugin.tx_kind_name(record.kind),
                    amount=f"{sign}{format_amount(abs(record.delta_minor))}",
                )
            )

        form = ActionForm(title=plugin.text("wallet.title"), content="\n".join(lines))
        form.add_button(plugin.text("common.back"), on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    def _show_audit(self, player) -> None:
        plugin = self.plugin
        audit = plugin.recovery.audit()
        lines = [plugin.text("admin.audit_line", k=k, v=v) for k, v in audit.items()]
        player.send_message(plugin.text("admin.audit_header") + "\n" + "\n".join(lines))

    def _reset_prices(self, player) -> None:
        plugin = self.plugin
        n = plugin.pricing.reset_adjustments()
        player.send_message(plugin.text("admin.reset_prices_done", n=n))

    def _daily(self, player) -> None:
        plugin = self.plugin
        n = plugin.pricing.maybe_apply_daily()
        player.send_message(plugin.text("admin.daily_done", n=n))
        player.send_message(plugin.pricing.daily_summary())

    def _reload(self, player) -> None:
        plugin = self.plugin
        try:
            plugin.reload_all()
            player.send_message(plugin.text("admin.reloaded"))
        except Exception as e:
            player.send_message(plugin.text("admin.reload_failed", err=str(e)))


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
