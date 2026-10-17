"""金额：一律用 int64 最小单位（minor），全项目禁用 float。

约定：1 金币 = 100 minor。税率与折扣用基点 bps（1% = 100 bps）。
所有取整都是确定性的（四舍五入到 minor，且同类运算顺序固定），
保证同一笔交易重放得到的结果完全一致 —— 这是账本能对账的前提。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

MINOR_PER_UNIT = 100
UNIT_SYMBOL = "金"
CURRENCY_NAME = "金币"

# 金额上限：约 92 亿金币，留足溢出余量
MAX_MINOR = 1 << 62

# 类型别名：金额一律是 int（最小单位 minor）。
# 不用自定义类包装，避免 isinstance 判断与 JSON 序列化上的额外复杂度。
Money = int
Minor = int

# ---------- 运行时货币配置 ----------
# 默认值在上面；插件装载时由 config 覆盖（economy.currency_name / currency_symbol /
# minor_per_unit），全项目统一从这里读，保证展示与换算口径一致。

_CURRENCY = {"name": CURRENCY_NAME, "symbol": UNIT_SYMBOL, "minor_per_unit": MINOR_PER_UNIT}


def configure_currency(
    *,
    name: Optional[str] = None,
    symbol: Optional[str] = None,
    per_unit: Optional[int] = None,
) -> None:
    """插件装载时注入服主自定义的货币名称与换算比（1 显示单位 = per_unit minor）。"""
    if name:
        _CURRENCY["name"] = str(name)
    if symbol:
        _CURRENCY["symbol"] = str(symbol)
    if per_unit is not None:
        _CURRENCY["minor_per_unit"] = max(1, int(per_unit))


def currency_name() -> str:
    return _CURRENCY["name"]


def currency_symbol() -> str:
    return _CURRENCY["symbol"]


def minor_per_unit() -> int:
    return _CURRENCY["minor_per_unit"]


def from_unit(value) -> int:
    """显示单位（int/float/str/Decimal）-> minor。"""
    per_unit = _CURRENCY["minor_per_unit"]
    if isinstance(value, int):
        return value * per_unit
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"非法金额: {value!r}")
    return int((dec * per_unit).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_unit(minor: int) -> Decimal:
    """minor -> 显示单位 Decimal（仅用于展示与校验，不参与计算）。"""
    return (Decimal(int(minor)) / Decimal(_CURRENCY["minor_per_unit"])).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def format_amount(minor: int, *, with_symbol: bool = True) -> str:
    """展示用：12345 -> '123.45金'（符号随货币配置）。"""
    text = f"{to_unit(minor):,}"
    return f"{text}{_CURRENCY['symbol']}" if with_symbol else text


def parse_amount(text: str) -> int:
    """玩家输入 -> minor。接受 '100' / '100.5' / '1,000'。失败抛 ValueError。"""
    if text is None:
        raise ValueError("金额为空")
    cleaned = str(text).strip().replace(",", "").replace(UNIT_SYMBOL, "")
    if not cleaned:
        raise ValueError("金额为空")
    return from_unit(cleaned)


def apply_bps(minor: int, bps: int) -> int:
    """按比例基点取值：apply_bps(10000, 500) = 5000（5%）。"""
    return (int(minor) * int(bps)) // 10000


def add_bps(minor: int, bps: int) -> int:
    """按基点加成/折扣后取整：add_bps(10000, -500) = 9500。"""
    return (int(minor) * (10000 + int(bps))) // 10000


def split_tax(minor: int, tax_bps: int) -> tuple[int, int]:
    """(净额, 税额)，保证 净额 + 税额 == 原额，绝不因取整丢钱。"""
    tax = apply_bps(minor, tax_bps)
    return int(minor) - tax, tax


def clamp_amount(minor: int) -> int:
    if minor < 0:
        return 0
    return min(int(minor), MAX_MINOR)


def is_valid(minor) -> bool:
    return isinstance(minor, int) and not isinstance(minor, bool) and 0 <= minor <= MAX_MINOR
