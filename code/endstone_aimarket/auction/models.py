"""拍卖系统的枚举与数据模型。"""
from __future__ import annotations

from enum import Enum


class AuctionKind(str, Enum):
    """拍卖形式。

    OFFLINE 离线拍：设最低价与截止时间，到点由系统结算，出价最高者得。
    LIVE    现场拍：短窗口 + 防狙击（每次出价延长倒计时），适合玩家聚在一起实时抢拍。
    """

    OFFLINE = "offline"
    LIVE = "live"


class AuctionStatus(str, Enum):
    OPEN = "open"          # 进行中，可出价
    SOLD = "sold"          # 已成交
    EXPIRED = "expired"    # 流拍：无人达到最低价，物品归还卖家
    CANCELLED = "cancelled"  # 卖家主动撤销（仅限尚无人出价）


# 每场拍卖在 escrow 账户里最多冻结的"当前最高出价"
