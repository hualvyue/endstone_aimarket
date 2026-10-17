"""背包服务：物品序列化、容量预算、发放与扣除。"""
from .item_codec import (
    ARMOR_SLOTS,
    make_item_stack,
    matches,
    match_key,
    max_stack_size,
    serialize_item,
    same_kind,
    with_count,
)
from .planner import TakePlan, count_items, estimate_capacity, has_room_for, plan_take
from .service import InventoryService

__all__ = [
    "InventoryService",
    "TakePlan",
    "ARMOR_SLOTS",
    "count_items",
    "estimate_capacity",
    "has_room_for",
    "plan_take",
    "make_item_stack",
    "matches",
    "match_key",
    "max_stack_size",
    "serialize_item",
    "same_kind",
    "with_count",
]
