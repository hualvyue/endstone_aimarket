"""NBT 编解码：把物品的完整 NBT 无损地存进数据库。

背景（这一层存在的唯一理由）
------------------------------------------------------------------
Endstone 0.11.x 的 `endstone.nbt` 只导出标签类，**没有 load()/dump()**，
所以无法走二进制往返，只能手动遍历标签树再重建。这里有两个必须避开的陷阱：

1. **不能用 `CompoundTag.to_dict()`**。它是有损的：ByteArrayTag 会变成 `bytes`，
   而 `json.dumps` 无法序列化 bytes；一旦序列化失败而调用方又吞掉异常，
   整份 NBT 就会**静默消失** —— 表现是潜影盒存进去再取出来内容是空的，
   但日志里什么都没有，极难排查。

2. **不能靠字段名猜数值类型**。基岩版 NBT 里 `lvl`/`id`（附魔）是 Short，
   烟花的 `Flight` 是 Byte。若一律按 Int 写回，客户端读到的就是 0 ——
   附魔等级变 0、烟花飞行时间变 0。

因此这里采用**自带类型标记**的表示法：写入时把标签的真实类型记下来，
读回时按标记还原，不做任何猜测。

    数值 -> {"@b"|"@s"|"@i"|"@l"|"@f"|"@d": 值}
    数组 -> {"@B": base64} / {"@I": [int, ...]}
    字符串、列表、复合标签保持自然形态

标记键以 `@` 开头，基岩版 NBT 字段名不会以 `@` 开头，因此不会冲突。
编码统一 `sort_keys=True`，保证同一份 NBT 每次编码结果一致 ——
这是「按 NBT 精确匹配物品」能成立的前提。
"""
from __future__ import annotations

import base64
import json
import threading
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 旧数据兼容：改动之前存下来的裸整数没有 @ 标记，只能按字段名猜类型。
# 新写入的数据一律带标记，不依赖这两张表。
# ---------------------------------------------------------------------------

_LEGACY_BYTE_FIELDS = frozenset({
    "Slot", "Count", "WasPickedUp", "inverted", "Findable", "KeepPacked", "OnGround",
    "Fire", "SpawnEgg", "waterlogged", "stripped_bit", "extinguished", "drag_down",
    "paused", "attached_bit", "disarmed_bit", "covered_bit", "upside_down_bit",
    "open_bit", "triggered_bit", "powered_bit", "toggle_bit", "occupied_bit",
    "in_wall_bit", "button_pressed_bit", "top_slot_bit", "conditional_bit",
    "update_bit", "door_hinge_bit", "upper_block_bit", "infiniburn_bit",
    "allow_underwater_bit", "brewing_stand_slot_a_bit", "brewing_stand_slot_b_bit",
    "brewing_stand_slot_c_bit",
})

_LEGACY_SHORT_FIELDS = frozenset({"Damage", "Health", "Age"})

_MARK_BYTE = "@b"
_MARK_SHORT = "@s"
_MARK_INT = "@i"
_MARK_LONG = "@l"
_MARK_FLOAT = "@f"
_MARK_DOUBLE = "@d"
_MARK_BYTE_ARRAY = "@B"
_MARK_INT_ARRAY = "@I"

_TYPE_NAMES = {
    "ByteTag": _MARK_BYTE,
    "ShortTag": _MARK_SHORT,
    "IntTag": _MARK_INT,
    "LongTag": _MARK_LONG,
    "FloatTag": _MARK_FLOAT,
    "DoubleTag": _MARK_DOUBLE,
    "ByteArrayTag": _MARK_BYTE_ARRAY,
    "IntArrayTag": _MARK_INT_ARRAY,
}


class NbtCodecError(Exception):
    pass


def nbt_to_plain(tag: Any) -> Any:
    """NBT 标签树 -> 可 JSON 序列化的结构（数值带类型标记）。"""
    if tag is None:
        return None
    cls = type(tag).__name__

    if cls == "CompoundTag":
        out: Dict[str, Any] = {}
        for key, value in tag.items():
            out[str(key)] = nbt_to_plain(value)
        return out

    if cls == "ListTag":
        return [nbt_to_plain(v) for v in tag]

    mark = _TYPE_NAMES.get(cls)
    if mark == _MARK_BYTE_ARRAY:
        return {mark: base64.b64encode(bytes(tag)).decode("ascii")}
    if mark == _MARK_INT_ARRAY:
        return {mark: [int(x) for x in tag]}
    if mark is not None:
        if mark in (_MARK_FLOAT, _MARK_DOUBLE):
            return {mark: float(tag.value)}
        return {mark: int(tag.value)}

    if cls == "StringTag":
        return str(tag.value)

    # 未知标签类型：尽量降级而不是抛异常，但要留下痕迹
    try:
        return tag.to_dict()
    except Exception:
        raise NbtCodecError(f"无法识别的 NBT 标签类型: {cls}")


def plain_to_nbt(value: Any, field_name: str = "") -> Any:
    """可 JSON 结构 -> NBT 标签树。优先按 @ 标记还原，无标记时按字段名兜底。"""
    from endstone.nbt import (  # 延迟导入：让本模块在无 Endstone 环境也能被静态检查
        ByteArrayTag,
        ByteTag,
        CompoundTag,
        DoubleTag,
        FloatTag,
        IntArrayTag,
        IntTag,
        ListTag,
        LongTag,
        ShortTag,
        StringTag,
    )

    if isinstance(value, dict):
        if len(value) == 1:
            mark, payload = next(iter(value.items()))
            if mark == _MARK_BYTE:
                return ByteTag(int(payload))
            if mark == _MARK_SHORT:
                return ShortTag(int(payload))
            if mark == _MARK_INT:
                return IntTag(int(payload))
            if mark == _MARK_LONG:
                return LongTag(int(payload))
            if mark == _MARK_FLOAT:
                return FloatTag(float(payload))
            if mark == _MARK_DOUBLE:
                return DoubleTag(float(payload))
            if mark == _MARK_BYTE_ARRAY:
                return ByteArrayTag(base64.b64decode(payload))
            if mark == _MARK_INT_ARRAY:
                return IntArrayTag([int(x) for x in payload])
        compound = CompoundTag()
        for key, item in value.items():
            compound[str(key)] = plain_to_nbt(item, str(key))
        return compound

    if isinstance(value, (list, tuple)):
        lst = ListTag()
        for element in value:
            lst.append(plain_to_nbt(element, field_name))
        return lst

    if isinstance(value, bool):
        return ByteTag(1 if value else 0)

    if isinstance(value, int):
        if field_name in _LEGACY_BYTE_FIELDS:
            return ByteTag(value)
        if field_name in _LEGACY_SHORT_FIELDS:
            return ShortTag(value)
        return IntTag(value)

    if isinstance(value, float):
        return DoubleTag(value)

    return StringTag(str(value))


def encode_nbt(tag: Any) -> Optional[str]:
    """NBT 标签树 -> base64(JSON)。空标签返回 None。编码失败**必须**让调用方知道。"""
    if tag is None:
        return None
    plain = nbt_to_plain(tag)
    if not plain:
        return None
    raw = json.dumps(
        plain, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_nbt(blob: str) -> Any:
    """base64(JSON) -> NBT 标签树。失败返回 None（调用方需自行决定是否放弃该物品）。"""
    if not blob:
        return None
    try:
        payload = json.loads(base64.b64decode(blob).decode("utf-8"))
        if not payload:
            return None
        return plain_to_nbt(payload)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 附魔读取
#
# ItemMeta.enchants 返回 dict[Enchantment, int]，键不可哈希会直接报错，
# 因此只能通过 get_enchant_level(id) 逐个已知 id 去问。
# ---------------------------------------------------------------------------

_ENCHANT_IDS: List[str] = []
_ENCHANT_LOCK = threading.Lock()

_FALLBACK_ENCHANT_IDS = [
    "minecraft:aqua_affinity", "minecraft:bane_of_arthropods", "minecraft:blast_protection",
    "minecraft:breach", "minecraft:channeling", "minecraft:binding", "minecraft:vanishing",
    "minecraft:density", "minecraft:depth_strider", "minecraft:efficiency",
    "minecraft:feather_falling", "minecraft:fire_aspect", "minecraft:fire_protection",
    "minecraft:flame", "minecraft:frost_walker", "minecraft:impaling", "minecraft:infinity",
    "minecraft:knockback", "minecraft:looting", "minecraft:loyalty", "minecraft:luck_of_the_sea",
    "minecraft:lure", "minecraft:mending", "minecraft:multishot", "minecraft:piercing",
    "minecraft:power", "minecraft:projectile_protection", "minecraft:protection",
    "minecraft:punch", "minecraft:quick_charge", "minecraft:respiration", "minecraft:riptide",
    "minecraft:sharpness", "minecraft:silk_touch", "minecraft:smite", "minecraft:soul_speed",
    "minecraft:swift_sneak", "minecraft:thorns", "minecraft:unbreaking", "minecraft:wind_burst",
    "minecraft:fortune", "minecraft:lunge", "minecraft:wind_burst",
]

# 基岩版旧版数字 id -> 字符串 id，用于从 NBT 的 ench 列表解析附魔书
_LEGACY_ENCHANT_NUM = {
    0: "minecraft:protection", 1: "minecraft:fire_protection", 2: "minecraft:feather_falling",
    3: "minecraft:blast_protection", 4: "minecraft:projectile_protection", 5: "minecraft:thorns",
    6: "minecraft:respiration", 7: "minecraft:depth_strider", 8: "minecraft:aqua_affinity",
    9: "minecraft:sharpness", 10: "minecraft:smite", 11: "minecraft:bane_of_arthropods",
    12: "minecraft:knockback", 13: "minecraft:fire_aspect", 14: "minecraft:looting",
    15: "minecraft:efficiency", 16: "minecraft:silk_touch", 17: "minecraft:unbreaking",
    18: "minecraft:fortune", 19: "minecraft:power", 20: "minecraft:punch",
    21: "minecraft:flame", 22: "minecraft:infinity", 23: "minecraft:luck_of_the_sea",
    24: "minecraft:lure", 25: "minecraft:frost_walker", 26: "minecraft:mending",
    27: "minecraft:binding", 28: "minecraft:vanishing", 29: "minecraft:impaling",
    30: "minecraft:riptide", 31: "minecraft:loyalty", 32: "minecraft:channeling",
    33: "minecraft:multishot", 34: "minecraft:piercing", 35: "minecraft:quick_charge",
    36: "minecraft:soul_speed", 37: "minecraft:swift_sneak",
}


def all_enchant_ids() -> List[str]:
    """收集服务器支持的全部附魔 id（只做一次）。"""
    global _ENCHANT_IDS
    if _ENCHANT_IDS:
        return _ENCHANT_IDS
    with _ENCHANT_LOCK:
        if _ENCHANT_IDS:
            return _ENCHANT_IDS
        found: List[str] = []
        try:
            from endstone.enchantments import Enchantment

            for name in dir(Enchantment):
                if not name.isupper():
                    continue
                value = getattr(Enchantment, name, None)
                if isinstance(value, str) and value.startswith("minecraft:"):
                    found.append(value)
        except Exception:
            found = []
        _ENCHANT_IDS = sorted(set(found)) or list(_FALLBACK_ENCHANT_IDS)
        return _ENCHANT_IDS


def normalize_enchant_id(enchant_id: str) -> str:
    """统一成 minecraft:xxx，兼容旧数据里的短 id。"""
    if not enchant_id:
        return enchant_id
    text = str(enchant_id)
    if text.startswith("minecraft:"):
        return text
    return "minecraft:" + text.replace(" ", "_").lower()


def legacy_enchant_name(number: int) -> Optional[str]:
    return _LEGACY_ENCHANT_NUM.get(int(number))
