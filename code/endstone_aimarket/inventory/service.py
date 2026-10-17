"""背包服务：唯一的背包读写入口。

设计原则：
1. **发物品返回「实际入包数量」**，而不是布尔值。这是部分成交（背包只能装下一部分）
   能正确结算的前提 —— 只报成败的话，背包一满就整单失败，账也没法对。
2. **扣物品先规划再执行**。`plan_take` 只读，确认够了才 `apply_take` 写入，
   中间不产生半扣状态；边遍历边改很容易在异常时留下不一致的背包。
3. 所有异常都被收敛成 Result，不让背包问题炸穿交易流程。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.errors import ErrorCode
from ..core.result import Result
from . import item_codec, nbt_codec, planner
from .item_codec import ARMOR_SLOTS


def leftover_count(leftover, *, requested: int) -> int:
    """把 `Inventory.add_item` 的返回值换算成「没能放进去的总数量」。

    这个函数的返回值直接决定交易的结算数量，错了就是资金事故，所以对返回形态
    一律做兼容，且在无法识别时**保守地认为全部没放进去** ——
    宁可让这笔交易按未成交处理，也不能多算发放量让玩家白付钱。

    已知形态：
        None / {} / []            -> 0（全部放入）
        {槽位或ItemStack: ItemStack} -> 累加各 value 的 amount
        [ItemStack, ...]          -> 累加 amount
        ItemStack                 -> 它自己的 amount
    """
    if not leftover:
        return 0

    if isinstance(leftover, dict):
        total = 0
        for value in leftover.values():
            total += int(getattr(value, "amount", 0) or 0)
        return total

    if isinstance(leftover, (list, tuple)):
        total = 0
        for value in leftover:
            total += int(getattr(value, "amount", 0) or 0)
        return total

    amount = getattr(leftover, "amount", None)
    if amount is not None:
        try:
            return int(amount or 0)
        except (TypeError, ValueError):
            return int(requested)

    # 未知形态：保守处理，等价于「一个都没放进去」
    return int(requested)


class InventoryService:
    def __init__(self, log=None):
        self.log = log

    # ==================== 读 ====================

    def get_items(
        self,
        player,
        *,
        server=None,
        include_armor: bool = False,
        slot_min: Optional[int] = None,
        slot_max: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """列出背包中所有非空物品。"""
        locale = getattr(player, "locale", None)
        items: List[Dict[str, Any]] = []
        for index, stack in planner.iter_main_slots(player, slot_min=slot_min, slot_max=slot_max):
            if not stack:
                continue
            try:
                info = item_codec.serialize_item(stack, server=server, locale=locale)
            except nbt_codec.NbtCodecError as e:
                if self.log:
                    self.log.error(f"读取背包物品失败（内容可能丢失）: {e}")
                continue
            if info:
                info["slot_index"] = index
                items.append(info)

        if include_armor:
            inventory = getattr(player, "inventory", None)
            for attr in ARMOR_SLOTS:
                try:
                    stack = getattr(inventory, attr, None)
                except Exception:
                    continue
                if not stack:
                    continue
                try:
                    info = item_codec.serialize_item(stack, server=server, locale=locale)
                except nbt_codec.NbtCodecError:
                    continue
                if info:
                    info["armor_slot"] = attr
                    items.append(info)
        return items

    def has(self, player, item_info: Dict[str, Any], count: int = 1, *, include_armor: bool = False) -> bool:
        return planner.count_items(player, item_info, include_armor=include_armor) >= int(count)

    def count(self, player, item_info: Dict[str, Any], *, include_armor: bool = False) -> int:
        return planner.count_items(player, item_info, include_armor=include_armor)

    def count_of_type(self, player, item_type: str) -> int:
        """按物品类型粗统计（忽略 NBT/附魔），用于「背包里有没有这类东西」的界面筛选。"""
        total = 0
        for _, stack in planner.iter_main_slots(player):
            if not stack:
                continue
            item_id = item_codec._type_id(stack)
            if item_id == item_type:
                total += int(getattr(stack, "amount", 0) or 0)
        return total

    def match_keys(self, player) -> set:
        """背包中所有物品的匹配键集合，用于批量筛选可交易物品。"""
        keys = set()
        for _, stack in planner.iter_main_slots(player):
            if not stack:
                continue
            try:
                info = item_codec.serialize_item(stack, with_nbt=True)
            except nbt_codec.NbtCodecError:
                continue
            if info:
                keys.add(item_codec.match_key(info))
        return keys

    def capacity_for(self, player, item_info: Dict[str, Any], *, include_armor: bool = False) -> int:
        return planner.estimate_capacity(player, item_info, include_armor=include_armor)

    def summary(self, player) -> Dict[str, Any]:
        return planner.summarize_inventory(player)

    # ==================== 扣物品 ====================

    def take(
        self,
        player,
        item_info: Dict[str, Any],
        count: int,
        *,
        include_armor: bool = False,
        slot_min: Optional[int] = None,
        slot_max: Optional[int] = None,
    ) -> Result[int]:
        """从背包扣走 count 个物品，返回**实际扣走的数量**。

        不足时不动背包（除非调用方传 partial 语义，这里保持严格语义：
        规划不够就直接失败，让上层决定是否降级为部分成交）。
        """
        wanted = int(count)
        if wanted <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        plan = planner.plan_take(
            player, item_info, wanted,
            include_armor=include_armor, slot_min=slot_min, slot_max=slot_max,
        )
        if plan.total < wanted:
            return Result.failure(
                ErrorCode.ITEM_NOT_ENOUGH, f"需要 {wanted}，背包只有 {plan.total}"
            )

        removed = self.apply_take(player, plan, item_info)
        if removed < wanted:
            return Result.failure(
                ErrorCode.ITEM_REMOVE_FAILED, f"计划扣 {wanted}，实际只扣了 {removed}"
            )
        return Result.success(removed)

    def plan_take(self, player, item_info, count, **kwargs):
        """暴露给交易协调器：先拿到计划，便于和数据库事务对齐。"""
        return planner.plan_take(player, item_info, count, **kwargs)

    def apply_take(self, player, plan, item_info: Dict[str, Any]) -> int:
        """执行 `plan_take` 的结果。"""
        inventory = getattr(player, "inventory", None)
        if inventory is None:
            return 0
        removed = 0
        for slot_index, take in plan.slots:
            try:
                stack = inventory.get_item(slot_index)
                if not stack:
                    continue
                available = int(getattr(stack, "amount", 0) or 0)
                actual = min(take, available)
                if actual <= 0:
                    continue
                if actual >= available:
                    inventory.set_item(slot_index, None)
                else:
                    stack.amount = available - actual
                    inventory.set_item(slot_index, stack)
                removed += actual
            except Exception as e:
                if self.log:
                    self.log.error(f"扣除背包槽 {slot_index} 失败: {e}")
        return removed

    # ==================== 发物品 ====================

    def give(
        self,
        player,
        item_info: Dict[str, Any],
        count: int,
        *,
        prefer_end: bool = False,
        reserved: int = 0,
        slot: Optional[int] = None,
        armor_slot: Optional[str] = None,
    ) -> Result[int]:
        """发放物品，返回**实际入包数量**（可能小于请求量）。"""
        wanted = int(count)
        if wanted <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)

        if armor_slot:
            return self._give_armor(player, item_info, armor_slot)
        if slot is not None:
            return self._give_slot(player, item_info, wanted, slot)

        # 发放前后各数一遍背包，用实测差值作为最终发放量 —— 这是最后一道防线：
        # 无论 _give_stacks 内部怎么算，都不会出现"账上多算、玩家少拿"的情况。
        before = self._count_matching(player, item_info)
        estimated = self._give_stacks(
            player, item_info, wanted, prefer_end=prefer_end, reserved=reserved
        )
        actual = self._count_matching(player, item_info) - before

        if actual != estimated:
            if self.log:
                self.log.error(
                    f"发放数量校验不一致（背包实测 {actual} / 计算 {estimated}），"
                    f"以实测为准。物品 {item_info.get('type')} 请求 {wanted}"
                )
            actual = max(0, min(actual, wanted))

        if actual <= 0:
            return Result.failure(ErrorCode.INVENTORY_FULL)
        return Result.success(actual)

    def _give_armor(self, player, item_info: Dict[str, Any], armor_slot: str) -> Result[int]:
        inventory = getattr(player, "inventory", None)
        if inventory is None or armor_slot not in ARMOR_SLOTS:
            return Result.failure(ErrorCode.INTERNAL, "无效的护甲槽")
        try:
            if getattr(inventory, armor_slot, None) is not None:
                return Result.failure(ErrorCode.INVENTORY_FULL, "该护甲槽已被占用")
            stack = item_codec.make_item_stack(item_codec.with_count(item_info, 1))
            setattr(inventory, armor_slot, stack)
            return Result.success(1)
        except Exception as e:
            return Result.failure(ErrorCode.ITEM_GIVE_FAILED, str(e))

    def _give_slot(self, player, item_info: Dict[str, Any], count: int, slot: int) -> Result[int]:
        inventory = getattr(player, "inventory", None)
        if inventory is None:
            return Result.failure(ErrorCode.INTERNAL, "无法访问背包")
        try:
            probe = item_codec.make_item_stack(item_codec.with_count(item_info, 1))
            max_stack = item_codec.max_stack_size(probe) if probe else 64
            amount = min(count, max(1, max_stack))
            stack = item_codec.make_item_stack(item_codec.with_count(item_info, amount))
            if stack is None:
                return Result.failure(ErrorCode.ITEM_GIVE_FAILED)
            existing = inventory.get_item(slot)
            if existing is not None:
                return Result.failure(ErrorCode.INVENTORY_FULL, "目标槽位不为空")
            inventory.set_item(slot, stack)
            return Result.success(amount)
        except Exception as e:
            return Result.failure(ErrorCode.ITEM_GIVE_FAILED, str(e))

    def _give_stacks(
        self, player, item_info: Dict[str, Any], count: int, *, prefer_end: bool, reserved: int
    ) -> int:
        """按堆叠上限分堆发放。先填同类堆，再占空槽，最后交给 add_item 兜底。"""
        inventory = getattr(player, "inventory", None)
        if inventory is None:
            return 0

        try:
            probe = item_codec.make_item_stack(item_codec.with_count(item_info, 1))
        except Exception as e:
            if self.log:
                self.log.error(f"构造物品失败: {e}")
            return 0
        if probe is None:
            return 0

        max_stack = max(1, item_codec.max_stack_size(probe))
        remaining = int(count)
        given = 0

        try:
            size = int(inventory.size)
        except Exception:
            return 0

        order = list(range(size))
        if prefer_end:
            order = list(range(size - 1, reserved - 1, -1)) + list(range(0, max(0, reserved)))

        # 第一轮：补到已有的同类堆里
        for index in order:
            if remaining <= 0:
                break
            try:
                stack = inventory.get_item(index)
            except Exception:
                continue
            if not stack or not item_codec.matches(stack, item_info):
                continue
            current = int(getattr(stack, "amount", 0) or 0)
            space = max_stack - current
            if space <= 0:
                continue
            add = min(space, remaining)
            try:
                stack.amount = current + add
                inventory.set_item(index, stack)
                remaining -= add
                given += add
            except Exception:
                continue

        # 第二轮：占用空槽
        for index in order:
            if remaining <= 0:
                break
            try:
                if inventory.get_item(index) is not None:
                    continue
            except Exception:
                continue
            add = min(max_stack, remaining)
            try:
                stack = item_codec.make_item_stack(item_codec.with_count(item_info, add))
                if stack is None:
                    continue
                inventory.set_item(index, stack)
                remaining -= add
                given += add
            except Exception:
                continue

        # 第三轮：交给服务端 API 兜底（会处理我们没考虑到的特殊情况）
        #
        # 注意 add_item 的返回值语义：它返回的是**放不下的残余**，
        # 形态可能是 None / 空 dict / dict{槽:ItemStack} / list[ItemStack] / ItemStack。
        # 早先这里写成 `getattr(leftover, "amount", 0)`，对 dict 恒为 0，
        # 于是"背包满、只放进去一部分"会被误判成"全部发放成功"，
        # 结果是货少发了钱却按全量扣 —— 玩家吃亏。下面统一用 leftover_count 换算。
        guard = 0
        while remaining > 0 and guard < 128:
            guard += 1  # 防御性上限，避免任何异常形态导致死循环
            add = min(max_stack, remaining)
            try:
                stack = item_codec.make_item_stack(item_codec.with_count(item_info, add))
                if stack is None:
                    break
                leftover = inventory.add_item(stack)
            except Exception as e:
                if self.log:
                    self.log.error(f"add_item 失败: {e}")
                break

            added = max(0, add - leftover_count(leftover, requested=add))
            if added <= 0:
                break  # 确实放不下了，再试结果一样
            given += added
            remaining -= added
        return given

    def _count_matching(self, player, item_info: Dict[str, Any]) -> int:
        """数一遍主背包里符合 item_info 的物品总数，用于发放后的守恒校验。"""
        total = 0
        for _, stack in planner.iter_main_slots(player):
            if stack and item_codec.matches(stack, item_info):
                total += int(getattr(stack, "amount", 0) or 0)
        return total

    # ==================== 槽位与快照 ====================

    def set_slot(self, player, slot: int, item_info: Optional[Dict[str, Any]]) -> bool:
        inventory = getattr(player, "inventory", None)
        if inventory is None:
            return False
        try:
            if item_info is None:
                inventory.set_item(int(slot), None)
            else:
                stack = item_codec.make_item_stack(item_info)
                if stack is None:
                    return False
                inventory.set_item(int(slot), stack)
            return True
        except Exception:
            return False

    def snapshot(self, player, *, include_armor: bool = True) -> Dict[str, Any]:
        """全量快照（保留空槽），用于交易前的风险备份与事务补偿。"""
        inventory = getattr(player, "inventory", None)
        slots: List[Any] = []
        size = 0
        if inventory is not None:
            try:
                size = int(inventory.size)
            except Exception:
                size = 0
            for index in range(size):
                try:
                    slot_info = item_codec.serialize_item(inventory.get_item(index))
                except Exception:
                    slot_info = None
                slots.append(slot_info)
        data: Dict[str, Any] = {"size": size, "slots": slots}
        if include_armor and inventory is not None:
            armor: Dict[str, Any] = {}
            for attr in ARMOR_SLOTS:
                try:
                    armor[attr] = item_codec.serialize_item(getattr(inventory, attr, None))
                except Exception:
                    armor[attr] = None
            data["armor"] = armor
        return data

    def restore(self, player, snapshot: Dict[str, Any], *, include_armor: bool = True) -> bool:
        inventory = getattr(player, "inventory", None)
        if inventory is None or not isinstance(snapshot, dict):
            return False
        try:
            inventory.clear()
        except Exception:
            pass

        slots = snapshot.get("slots")
        if slots is None:
            # 兼容 {inventory: [...], armor: {...}} 的嵌套写法
            slots = snapshot.get("inventory") or []
        for index, slot_info in enumerate(slots):
            try:
                if slot_info is None:
                    inventory.set_item(index, None)
                else:
                    stack = item_codec.make_item_stack(slot_info)
                    if stack is not None:
                        inventory.set_item(index, stack)
            except Exception:
                continue

        if include_armor:
            armor = snapshot.get("armor") or {}
            for attr in ARMOR_SLOTS:
                if attr not in armor:
                    continue
                try:
                    setattr(inventory, attr, None)
                    info = armor.get(attr)
                    if info:
                        setattr(inventory, attr, item_codec.make_item_stack(info))
                except Exception:
                    continue
        return True

    # ==================== 序列化 ====================

    def serialize(self, item_stack: Any, *, server=None, locale: str = None) -> Optional[Dict[str, Any]]:
        return item_codec.serialize_item(item_stack, server=server, locale=locale)

    def deserialize(self, item_info: Dict[str, Any]) -> Any:
        return item_codec.make_item_stack(item_info)

    def matches(self, item_stack: Any, item_info: Dict[str, Any]) -> bool:
        return item_codec.matches(item_stack, item_info)

    def withdraw_for_give(self, player, item_info: Dict[str, Any], count: int) -> Result[int]:
        """便捷：确认能装下才扣走（用于「先装货后付款」的保守校验）。"""
        if not planner.has_room_for(player, item_info, count):
            return Result.failure(ErrorCode.INVENTORY_FULL)
        return self.take(player, item_info, count)
