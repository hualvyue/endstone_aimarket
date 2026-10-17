"""ItemStack <-> item_info 字典 的转换与匹配。

item_info 结构（商店、仓库、待发物品统一用这一份）：
    {
      "type":       "minecraft:diamond",   # 物品类型 id
      "name":       "钻石",                 # 展示名（优先玩家语言）
      "count":      64,                     # 数量
      "data":       0,                       # 特殊值
      "enchants":   {"minecraft:sharpness": 5},
      "lore":       ["第一行", "第二行"],
      "nbt_b64":    "...",                   # 可选，完整 NBT（存有内容物的潜影盒等）
      "slot_index": 3                        # 可选，仅用于「从背包读取」时回填
    }

匹配规则：
  * 必比 type + data；
  * **存在 nbt_b64 时只比 NBT 全等**（NBT 已经包含附魔与名字，再比附魔反而会漏匹配）；
  * 否则比附魔（id 归一化）+ Lore。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import nbt_codec
from .nbt_codec import all_enchant_ids, encode_nbt, legacy_enchant_name, normalize_enchant_id

ARMOR_SLOTS = ("helmet", "chestplate", "leggings", "boots", "item_in_off_hand")


def _type_id(item_stack: Any) -> str:
    item_type = getattr(item_stack, "type", None)
    if item_type is None:
        return ""
    return str(getattr(item_type, "id", item_type) or "")


def _translation_key(item_stack: Any) -> Optional[str]:
    try:
        key = getattr(item_stack.type, "translation_key", None)
        return str(key) if key else None
    except Exception:
        return None


def read_enchants(item_stack: Any) -> Dict[str, int]:
    """读取附魔。不访问 ItemMeta.enchants（会因键不可哈希而报错）。"""
    result: Dict[str, int] = {}
    if not item_stack:
        return result

    meta = getattr(item_stack, "item_meta", None)
    get_level = getattr(meta, "get_enchant_level", None) if meta is not None else None
    if callable(get_level):
        for enchant_id in all_enchant_ids():
            try:
                level = get_level(enchant_id)
                if level and int(level) > 0:
                    result[enchant_id] = int(level)
            except Exception:
                continue

    # 附魔书常常 has_enchants=False 但 NBT 里有 ench，需要额外兜一层
    if not result:
        result = _read_enchants_from_nbt(item_stack)
    return result


def _read_enchants_from_nbt(item_stack: Any) -> Dict[str, int]:
    result: Dict[str, int] = {}
    try:
        nbt = getattr(item_stack, "nbt", None)
        if nbt is None:
            return result
        ench = None
        try:
            ench = nbt.get("ench") if hasattr(nbt, "get") else None
        except Exception:
            ench = None
        if ench is None:
            try:
                ench = nbt["ench"]
            except Exception:
                return result
        if ench is None:
            return result

        for entry in list(ench):
            try:
                raw_id = entry.get("id") if hasattr(entry, "get") else None
                raw_lvl = entry.get("lvl") if hasattr(entry, "get") else None
                if raw_id is None or raw_lvl is None:
                    continue
                if hasattr(raw_id, "value"):
                    raw_id = raw_id.value
                if hasattr(raw_lvl, "value"):
                    raw_lvl = raw_lvl.value
                level = int(raw_lvl)
                if level <= 0:
                    continue
                if isinstance(raw_id, str):
                    enchant_id = normalize_enchant_id(raw_id)
                else:
                    enchant_id = legacy_enchant_name(int(raw_id))
                if enchant_id:
                    result[enchant_id] = level
            except Exception:
                continue
    except Exception:
        return {}
    return result


def read_lore(item_stack: Any) -> List[str]:
    if not item_stack:
        return []
    meta = getattr(item_stack, "item_meta", None)
    if meta is None or not getattr(meta, "has_lore", False):
        return []
    try:
        lore = meta.lore
        return [str(line) for line in lore] if isinstance(lore, list) else []
    except Exception:
        return []


def read_display_name(item_stack: Any, server=None, locale: str = None) -> str:
    """优先用服务器语言包翻译；失败则退回类型名。"""
    if not item_stack:
        return "?"
    try:
        meta = getattr(item_stack, "item_meta", None)
        if meta is not None and getattr(meta, "has_display_name", False):
            display = getattr(meta, "display_name", None)
            if display:
                return str(display)
    except Exception:
        pass

    key = _translation_key(item_stack)
    if server is not None and key:
        try:
            translated = server.language.translate(key, None, locale)
            if translated:
                return str(translated)
        except Exception:
            pass

    item_id = _type_id(item_stack)
    short = item_id.split(":")[-1] if ":" in item_id else item_id
    return short.replace("_", " ").title() or "?"


def serialize_item(item_stack: Any, *, server=None, locale: str = None, with_nbt: bool = True) -> Optional[Dict[str, Any]]:
    """ItemStack -> item_info。空物品返回 None。"""
    if not item_stack:
        return None
    item_type = _type_id(item_stack)
    if not item_type:
        return None

    info: Dict[str, Any] = {
        "type": item_type,
        "name": read_display_name(item_stack, server, locale),
        "count": int(getattr(item_stack, "amount", 1) or 1),
        "data": int(getattr(item_stack, "data", 0) or 0),
        "enchants": read_enchants(item_stack),
        "lore": read_lore(item_stack),
    }

    key = _translation_key(item_stack)
    if key:
        info["type_translation_key"] = key

    if with_nbt:
        try:
            blob = encode_nbt(getattr(item_stack, "nbt", None))
            if blob:
                info["nbt_b64"] = blob
        except Exception as exc:
            # 编码失败意味着物品内容（潜影盒等）会丢失，必须显式暴露而不是静默吞掉
            raise nbt_codec.NbtCodecError(
                f"NBT 编码失败，物品 {item_type} 的内容将无法保存: {exc}"
            )
    return info


def make_item_stack(item_info: Dict[str, Any]) -> Any:
    """item_info -> ItemStack。"""
    try:
        # 正确位置：endstone.inventory.ItemStack（0.11 官方绑定）
        from endstone.inventory import ItemStack
    except ImportError:  # 兼容可能把 ItemStack 放在顶层的旧版本
        from endstone import ItemStack

    item_type = item_info.get("type")
    if not item_type:
        return None
    count = int(item_info.get("count", 1) or 1)
    stack = ItemStack(str(item_type), max(1, count))

    data = item_info.get("data")
    if data:
        try:
            stack.data = int(data)
        except Exception:
            pass

    blob = item_info.get("nbt_b64")
    if blob:
        restored = nbt_codec.decode_nbt(blob)
        if restored is not None:
            try:
                stack.nbt = restored
            except Exception:
                pass

    # NBT 已经带上附魔时不要重复写，否则会覆盖掉更完整的原始数据
    # 注意：item_meta 是只读副本，修改后必须 set_item_meta 写回，否则附魔/lore 静默丢失
    if not blob and (item_info.get("enchants") or item_info.get("lore")):
        try:
            meta = getattr(stack, "item_meta", None)
            if meta is None:
                try:
                    from endstone.inventory import ItemMeta

                    meta = ItemMeta()
                except Exception:
                    meta = None
            if meta is not None:
                changed = False
                for enchant_id, level in (item_info.get("enchants") or {}).items():
                    try:
                        meta.add_enchant(normalize_enchant_id(enchant_id), int(level), True)
                        changed = True
                    except TypeError:
                        # 旧版 API 没有 force 参数
                        try:
                            meta.add_enchant(normalize_enchant_id(enchant_id), int(level))
                            changed = True
                        except Exception:
                            continue
                    except Exception:
                        continue
                if item_info.get("lore"):
                    try:
                        meta.lore = [str(line) for line in item_info["lore"]]
                        changed = True
                    except Exception:
                        pass
                if changed:
                    stack.set_item_meta(meta)
        except Exception:
            pass

    return stack


def matches(stack: Any, item_info: Dict[str, Any]) -> bool:
    """判断一个 ItemStack 是否满足 item_info 的规格。"""
    if not stack or not item_info:
        return False
    if _type_id(stack) != item_info.get("type"):
        return False
    if int(getattr(stack, "data", 0) or 0) != int(item_info.get("data", 0) or 0):
        return False

    required_nbt = item_info.get("nbt_b64")
    if required_nbt:
        # 有完整 NBT 时，NBT 全等即可判定，不再比附魔/Lore
        try:
            actual = encode_nbt(getattr(stack, "nbt", None))
        except Exception:
            return False
        return actual == required_nbt

    required_enchants = item_info.get("enchants") or {}
    if required_enchants:
        actual_enchants = read_enchants(stack)
        for enchant_id, level in required_enchants.items():
            if actual_enchants.get(normalize_enchant_id(enchant_id)) != int(level):
                return False

    required_lore = item_info.get("lore") or []
    if required_lore:
        actual_lore = read_lore(stack)
        if len(actual_lore) != len(required_lore):
            return False
        for expect, actual in zip(required_lore, actual_lore):
            if str(expect) != str(actual):
                return False
    return True


def same_kind(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """判断两件物品是否属于「可以堆叠在一起」的同一种。

    注意堆叠规则是**类型 + 特殊值 + 完整 NBT** 三者都一致；
    只有 type+data 一致就允许堆叠，会把潜影盒里的东西混在一起。
    """
    if not a or not b:
        return False
    if a.get("type") != b.get("type"):
        return False
    if int(a.get("data", 0) or 0) != int(b.get("data", 0) or 0):
        return False
    return a.get("nbt_b64") == b.get("nbt_b64")


def max_stack_size(stack: Any) -> int:
    """读取物品自身的堆叠上限（工具 = 1，普通物品 = 64）。失败按 64。"""
    for attr in ("max_stack_size", "max_stack"):
        try:
            value = getattr(stack, attr, None)
            if value:
                return int(value)
        except Exception:
            continue
    return 64


def with_count(item_info: Dict[str, Any], count: int) -> Dict[str, Any]:
    """复制一份并改数量，避免调用方原地修改共享数据。"""
    out = dict(item_info or {})
    out["count"] = int(count)
    return out


def match_key(item_info: Dict[str, Any]) -> str:
    """给物品算一个可以放进 set 的匹配键，用于「背包里有没有这类东西」的快速判断。"""
    if not item_info:
        return ""
    if item_info.get("nbt_b64"):
        return f"{item_info.get('type')}|{item_info.get('data', 0)}|{item_info['nbt_b64']}"
    enchants = item_info.get("enchants") or {}
    ench_text = ",".join(f"{k}={v}" for k, v in sorted(enchants.items()))
    return f"{item_info.get('type')}|{item_info.get('data', 0)}|{ench_text}"
