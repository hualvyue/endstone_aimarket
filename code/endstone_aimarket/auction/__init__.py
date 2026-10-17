"""拍卖系统：现场拍 / 离线拍。

* service    —— 创建 / 出价 / 结算 / 流拍 / 取消，手续费进国库，每场独立托管
* repository —— 持久化（auctions / auction_bids）
* commands   —— /auction 文本与 GUI 入口
* forms      —— 市场面板、上架向导（背包快速选物）、出价、我的拍卖
* models     —— 状态与类型枚举

与商店/央行同口径：资金走复式记账，物品走 InventoryService，绝不直接改余额。
"""
from .commands import AuctionCommands, COMMANDS, PERMISSIONS
from .forms import AuctionForms
from .models import AuctionKind, AuctionStatus
from .repository import AuctionRepository
from .service import AuctionService

__all__ = [
    "AuctionService",
    "AuctionForms",
    "AuctionCommands",
    "AuctionRepository",
    "AuctionKind",
    "AuctionStatus",
    "COMMANDS",
    "PERMISSIONS",
]
