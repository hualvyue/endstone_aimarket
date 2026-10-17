"""按钮交互与方块保护。

Bedrock 的一个坑：左键点按钮会**先**触发 Interact 事件再触发 BlockBreak，
如果只按 action 判断而不区分左右键，玩家想拆店时会弹出商店详情页。
所以这里对 LEFT_CLICK 单独处理 —— 店主看到删除确认，其他人被拦下并提示。
"""
from typing import Any, Optional

try:  # Endstone 0.11 的事件命名空间
    from endstone.event import (
        BlockBreakEvent,
        EventPriority,
        PlayerInteractEvent,
        event_handler,
    )
except ImportError:  # pragma: no cover - 兼容极少数打包差异
    BlockBreakEvent = None
    PlayerInteractEvent = None
    EventPriority = None

    def event_handler(*args, **kwargs):
        def wrapper(fn):
            return fn

        return wrapper


BUTTON_BLOCKS = frozenset({
    "minecraft:wooden_button", "minecraft:stone_button", "minecraft:birch_button",
    "minecraft:spruce_button", "minecraft:jungle_button", "minecraft:acacia_button",
    "minecraft:dark_oak_button", "minecraft:mangrove_button", "minecraft:cherry_button",
    "minecraft:bamboo_button", "minecraft:crimson_button", "minecraft:warped_button",
    "minecraft:polished_blackstone_button", "minecraft:pale_oak_button",
})


def is_button(block) -> bool:
    if block is None:
        return False
    block_type = getattr(block, "type", None)
    if block_type is None:
        return False
    block_id = str(getattr(block_type, "id", block_type) or "")
    return block_id in BUTTON_BLOCKS


class ShopListener:
    """把方块事件翻译成商店动作。真正的业务在 ShopService / TradeCoordinator 里。"""

    def __init__(self, plugin):
        self.plugin = plugin

    # ==================== 右键 / 左键 ====================

    @event_handler(priority=EventPriority.NORMAL)
    def on_player_interact(self, event: PlayerInteractEvent) -> None:
        if PlayerInteractEvent is None:
            return
        try:
            self._handle_interact(event)
        except Exception as e:
            self.plugin.log.error(f"处理按钮交互失败: {e}")

    def _handle_interact(self, event) -> None:
        plugin = self.plugin
        player = event.player
        block = event.block
        if block is None or not is_button(block):
            return

        # 正在建店：只有右键才落点，其他按键忽略
        if plugin.is_building(player):
            if event.action != PlayerInteractEvent.Action.RIGHT_CLICK_BLOCK:
                return
            event.is_cancelled = True
            plugin.forms.start_build_at(player, block)
            return

        shop = plugin.shop_service.get_at(block.x, block.y, block.z, block.dimension.name)
        if not shop:
            return

        action = event.action
        if action == PlayerInteractEvent.Action.LEFT_CLICK_BLOCK:
            event.is_cancelled = True
            if not plugin.is_shop_active(shop):
                return
            if plugin.can_manage(player, shop):
                plugin.forms.confirm_delete_shop(player, shop)
            else:
                player.send_message(plugin.text("shop.break_protected", owner=plugin.owner_display(shop)))
            return

        if action == PlayerInteractEvent.Action.RIGHT_CLICK_BLOCK:
            event.is_cancelled = True
            plugin.forms.open_shop_detail(player, shop)

    # ==================== 破坏保护 ====================

    @event_handler(priority=EventPriority.NORMAL)
    def on_block_break(self, event: BlockBreakEvent) -> None:
        if BlockBreakEvent is None:
            return
        try:
            self._handle_break(event)
        except Exception as e:
            self.plugin.log.error(f"处理破坏事件失败: {e}")

    def _handle_break(self, event) -> None:
        plugin = self.plugin
        block = event.block
        if block is None or not is_button(block):
            return
        shop = plugin.shop_service.get_at(block.x, block.y, block.z, block.dimension.name)
        if not shop:
            return
        event.is_cancelled = True
        player = event.player
        if not plugin.is_shop_active(shop):
            return
        if plugin.can_manage(player, shop):
            plugin.forms.confirm_delete_shop(player, shop)
        else:
            player.send_message(plugin.text("shop.break_protected", owner=plugin.owner_display(shop)))
