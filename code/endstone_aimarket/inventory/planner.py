"""背包容量预算与移除计划。

为什么需要这一层：
交易的 Try 阶段必须先知道「玩家实际能吃下多少」和「实际能扣走多少」，
才能算出准确的冻结金额。如果先发货再清点实发数量，
钱和货的账要在发货之后才对齐，中途任何一步失败都会留下烂账。

这里把「能装多少 / 能扣多少」变成可预先计算的纯函数，
交易协调器据此决定请求量与冻结额度，做到 Try 阶段就锁定契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import item_codec
from .item_codec import ARMOR_SLOTS


@dataclass
class TakePlan:
    """从背包移除物品的计划：哪些槽位各取走几个。"""

    slots: List[tuple] = field(default_factory=list)  # [(slot_index, take_count), ...]
    airborne: int = 0  # 护甲/副手等非主背包来源的数量
    total: int = 0

    @property
    def is_empty(self) -> bool:
        return self.total <= 0

    def describe(self) -> str:
        return ", ".join(f"槽{i}x{n}" for i, n in self.slots) or "无"


def _main_inventory(player):
    return getattr(player, "inventory", None)


def iter_main_slots(player, *, slot_min: Optional[int] = None, slot_max: Optional[int] = None):
    """遍历主背包槽位，产出 (slot_index, item_stack)。"""
    inventory = _main_inventory(player)
    if inventory is None:
        return
    try:
        size = int(inventory.size)
    except Exception:
        return
    low = 0 if slot_min is None else max(0, int(slot_min))
    high = size - 1 if slot_max is None else min(size - 1, int(slot_max))
    for index in range(low, high + 1):
        try:
            yield index, inventory.get_item(index)
        except Exception:
            continue


def count_items(player, item_info: Dict[str, Any], *, include_armor: bool = False,
                slot_min: Optional[int] = None, slot_max: Optional[int] = None) -> int:
    """统计背包中符合条件的物品总数。"""
    total = 0
    for _, stack in iter_main_slots(player, slot_min=slot_min, slot_max=slot_max):
        if stack and item_codec.matches(stack, item_info):
            total += int(getattr(stack, "amount", 0) or 0)
    if include_armor:
        for attr in ARMOR_SLOTS:
            try:
                stack = getattr(_main_inventory(player), attr, None)
            except Exception:
                continue
            if stack and item_codec.matches(stack, item_info):
                total += int(getattr(stack, "amount", 0) or 0)
    return total


def plan_take(
    player,
    item_info: Dict[str, Any],
    count: int,
    *,
    include_armor: bool = False,
    slot_min: Optional[int] = None,
    slot_max: Optional[int] = None,
) -> TakePlan:
    """规划从背包扣走 count 个物品。

    只做规划、不动背包 —— 真正的写入由调用方在确认能成功后一次完成。
    """
    plan = TakePlan()
    wanted = max(0, int(count))
    if wanted <= 0:
        return plan

    for index, stack in iter_main_slots(player, slot_min=slot_min, slot_max=slot_max):
        if wanted <= 0:
            break
        if not stack or not item_codec.matches(stack, item_info):
            continue
        available = int(getattr(stack, "amount", 0) or 0)
        if available <= 0:
            continue
        take = min(available, wanted)
        plan.slots.append((index, take))
        wanted -= take

    plan.total = sum(n for _, n in plan.slots)
    return plan


def estimate_capacity(player, item_info: Dict[str, Any], *, include_armor: bool = False) -> int:
    """估算背包还能装下多少个该物品。

    空槽按该物品的堆叠上限计；已存在同类物品的槽位按剩余堆叠空间计。
    用途是给玩家一个「最多能买几个」的上限提示，并让 Try 阶段有据可依。
    """
    inventory = _main_inventory(player)
    if inventory is None or not item_info:
        return 0

    try:
        probe = item_codec.make_item_stack(item_codec.with_count(item_info, 1))
        max_stack = item_codec.max_stack_size(probe) if probe else 64
    except Exception:
        max_stack = 64
    max_stack = max(1, max_stack)

    capacity = 0
    for _, stack in iter_main_slots(player):
        if stack is None:
            capacity += max_stack
            continue
        if item_codec.matches(stack, item_info):
            existing = int(getattr(stack, "amount", 0) or 0)
            capacity += max(0, max_stack - existing)

    if include_armor:
        for attr in ARMOR_SLOTS:
            try:
                stack = getattr(inventory, attr, None)
            except Exception:
                continue
            if stack is None:
                capacity += 1  # 护甲槽只能放 1 件
    return capacity


def has_room_for(player, item_info: Dict[str, Any], count: int, *, include_armor: bool = False) -> bool:
    return estimate_capacity(player, item_info, include_armor=include_armor) >= int(count)


def summarize_inventory(player) -> Dict[str, Any]:
    """背包概览，给 UI 与指标采集用。"""
    used = 0
    empty = 0
    stacks = 0
    for _, stack in iter_main_slots(player):
        if stack is None:
            empty += 1
            continue
        used += 1
        stacks += int(getattr(stack, "amount", 0) or 0)
    inventory = _main_inventory(player)
    try:
        size = int(inventory.size)
    except Exception:
        size = used + empty
    return {"size": size, "used_slots": used, "empty_slots": empty, "total_items": stacks}
