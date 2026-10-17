"""交易协调器的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from ..core.errors import ErrorCode


class SagaPhase(str, Enum):
    """背包副作用的执行阶段，决定崩溃后该怎么收尾。

    ARMED    —— 已冻结，**尚未**触碰背包（安全，可直接取消）
    APPLYING —— 即将或已经触碰背包（状态不确定，必须人工裁决）
    DONE     —— 已结算完毕
    """

    ARMED = "armed"
    APPLYING = "applying"
    DONE = "done"


class TradeStatus(str, Enum):
    APPLYING = "applying"
    FILLED = "filled"
    CANCELLED = "cancelled"
    MANUAL = "manual"


@dataclass
class TradeRequest:
    """一次交易请求。由商店层构造，交给协调器执行。"""

    player: Any
    shop: Dict[str, Any]
    actor_account_id: str
    direction: str                       # 'buy' = 玩家买入, 'sell' = 玩家卖出
    quantity: int
    idempotency_key: str
    unit_minor: int = 0                  # 0 表示由协调器按官方价实时报价
    price_version: Optional[int] = None
    tax_bps: int = 0
    allow_partial: bool = True           # 装不下时是否降级为部分成交


@dataclass
class TradeOutcome:
    ok: bool
    code: ErrorCode = ErrorCode.OK
    message: str = ""
    trade_id: str = ""
    requested_qty: int = 0
    filled_qty: int = 0
    unit_minor: int = 0
    price_version: Optional[int] = None
    gross_minor: int = 0
    tax_minor: int = 0
    paid_minor: int = 0                  # 玩家实际付出
    received_minor: int = 0              # 玩家实际收到
    refunded_minor: int = 0              # 未成交部分释放掉的冻结额
    item_name: str = ""
    counterparty: str = ""
    replay: bool = False                 # 幂等重放
    flags: list = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        return 0 < self.filled_qty < self.requested_qty
