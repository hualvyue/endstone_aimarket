"""全局常量与枚举。

放在这里是为了让「账户类型 / 交易方向 / 商店类型」这些跨模块概念只有一份定义，
避免同样的字符串常量散落在各处硬编码、改一处漏一处。
"""
from __future__ import annotations

from enum import Enum

PLUGIN_ID = "aimarket"
DATA_DIR_NAME = "aimarket"
LOG_PREFIX = "Aimarket"

# 货币
CURRENCY_CODE = "AMC"
CURRENCY_NAME = "金币"


class AccountKind(str, Enum):
    """账户类型。玩家、公司、系统账户同为账户表的一等公民。

    **COMPANY 是「有限责任」的结构性保证**：accounts 表有约束
    `CHECK (balance_minor >= 0 OR kind = 'system')`，公司不属于 system，
    所以公司账户在数据库层面永远不可能变成负数 —— 债务无法被记到公司账上，
    缺口只可能由系统账户核销，从物理上杜绝了「公司欠债牵连股东个人」。
    """

    PLAYER = "player"
    COMPANY = "company"     # 公司法人账户（禁负 → 有限责任）
    TREASURY = "treasury"   # 国库（央行·财政科目）：税收与销毁回收
    ESCROW = "escrow"       # 商店托管：收购店的资金池
    SYSTEM = "system"       # 央行·发行科目 / 无限商店对手方 / 贷款投放方
    DEPOSIT = "deposit"     # 央行·存款科目：每一笔存款一个账户（禁负）

    # —— 中央银行体系：央行 = 发行(sys:mint) + 财政(sys:treasury) + 储备 ——
    # 三家合一，在 /bank 面板呈现为一张资产负债表。
    # 之所以不把 sys:mint 与 sys:treasury 物理合并：账本里每条分录都存着
    # 当时的余额快照，搬钱会让历史快照对不上、审计报警。语义合并零风险。


class CompanyStatus(str, Enum):
    ACTIVE = "active"
    LIQUIDATING = "liquidating"  # 清算中
    BANKRUPT = "bankrupt"        # 已破产
    CLOSED = "closed"            # 主动注销


class CompanyRole(str, Enum):
    FOUNDER = "founder"   # 法人代表：有权代表公司操作
    SHAREHOLDER = "shareholder"  # 普通股东：只能看与分红


class LoanStatus(str, Enum):
    ACTIVE = "active"            # 正常还款期
    OVERDUE = "overdue"          # 已逾期，宽限期内
    COLLECTING = "collecting"    # 追债中（个人：没收+变卖；公司：清算）
    SETTLED = "settled"          # 已结清
    DEFAULTED = "defaulted"      # 已违约处置完毕
    WRITTEN_OFF = "written_off"  # 坏账核销


class ShareStatus(str, Enum):
    LISTED = "listed"
    SUSPENDED = "suspended"   # 停牌（如清算中）
    DELISTED = "delisted"     # 已退市


class AccountStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class TxKind(str, Enum):
    """交易种类，决定账本分录的形态。"""

    MINT = "mint"                  # 铸造（系统 -> 玩家）
    BURN = "burn"                  # 销毁（玩家 -> 系统）
    TRANSFER = "transfer"          # 玩家间转账
    SHOP_BUY = "shop_buy"          # 玩家从商店买东西（玩家 -> 店主）
    SHOP_SELL = "shop_sell"        # 玩家卖东西给商店（托管 -> 玩家）
    TAX = "tax"                    # 税收（玩家 -> 国库）
    SHOP_RESTOCK = "shop_restock"  # 店主给收购店注入预算
    SHOP_WITHDRAW = "shop_withdraw"  # 店主从商店取回资金
    ADMIN = "admin"                # 管理员调整
    REFUND = "refund"              # 补偿/退款

    # 公司
    COMPANY_CAPITAL = "company_capital"      # 实缴注册资本（发起人 -> 公司）
    COMPANY_DIVIDEND = "company_dividend"    # 分红（公司 -> 各股东）
    COMPANY_LICENSE = "company_license"      # 个人开店牌照费（玩家 -> 国库）
    TURNOVER_TAX = "turnover_tax"            # 公司营业额税（公司 -> 国库）
    COMPANY_LIQUIDATE = "company_liquidate"  # 破产清算分配

    # 贷款
    LOAN_DISBURSE = "loan_disburse"          # 放贷（系统增发 -> 借款人）
    LOAN_REPAY = "loan_repay"                # 还款（借款人 -> 系统 + 利息进国库）
    LOAN_INTEREST = "loan_interest"          # 利息计提（仅记账，不移动本金）
    LOAN_SEIZE = "loan_seize"                # 追债变卖所得抵债
    LOAN_WRITEOFF = "loan_writeoff"          # 坏账核销（系统承担）

    # 股票
    SHARE_ISSUE = "share_issue"              # 申购（玩家 -> 公司，同时增股本）
    SHARE_REDEEM = "share_redeem"            # 赎回（公司 -> 玩家，同时减股本）

    # 央行·存款
    BANK_DEPOSIT_IN = "bank_deposit_in"      # 存入（玩家 -> 央行）
    BANK_DEPOSIT_OUT = "bank_deposit_out"    # 支取（央行 -> 玩家）
    BANK_INTEREST = "bank_interest"          # 存款利息（央行增发 -> 存款账户）

    # 央行·信贷
    BANK_OVERDRAFT_DRAW = "bank_overdraft_draw"      # 透支放款
    BANK_OVERDRAFT_REPAY = "bank_overdraft_repay"    # 透支归还
    BANK_CARD_CHARGE = "bank_card_charge"            # 信用卡消费（放款）
    BANK_CARD_REPAY = "bank_card_repay"              # 信用卡还款
    BANK_MORTGAGE_DISBURSE = "bank_mortgage"         # 按揭放款
    BANK_MORTGAGE_REPAY = "bank_mortgage_repay"      # 按揭还款
    BANK_MORTGAGE_FORECLOSE = "bank_mortgage_foreclose"  # 违约收走抵押物
    BANK_DISCOUNT = "bank_discount"                  # 再贴现放款

    # 央行·公开市场与监管
    BANK_OMO_BUY = "bank_omo_buy"            # 公开市场买入（投放货币）
    BANK_OMO_SELL = "bank_omo_sell"          # 公开市场卖出（回收货币）
    BANK_WRITEOFF = "bank_writeoff"          # 坏账核销（央行资本减记）

    # 拍卖
    AUCTION_FEE = "auction_fee"              # 拍卖手续费（卖家 -> 国库）
    AUCTION_BID = "auction_bid"              # 出价（买家 -> 拍卖托管）
    AUCTION_BID_REFUND = "auction_bid_refund"  # 被超过后退回出价（托管 -> 原出价者）
    AUCTION_SALE = "auction_sale"            # 成交（托管 -> 卖家）


class TxStatus(str, Enum):
    PENDING = "pending"
    APPLYING = "applying"   # 已冻结，正在执行背包副作用
    FILLED = "filled"       # 已完成
    CANCELLED = "cancelled"
    MANUAL = "manual"       # 状态不明，冻结保持，等待 OP 裁决


# ==================== 央行体系 ====================


class BankProduct(str, Enum):
    """央行对外产品的种类。利率都由基准利率加利差派生，行长只需调一个基准。"""

    DEMAND = "demand"          # 活期存款
    TIME = "time"              # 定期存款
    WEALTH = "wealth"          # 理财（本轮只落端口，策略待接 AI）
    OVERDRAFT = "overdraft"    # 透支（授信额度 + 自动放款）
    CARD = "card"              # 信用卡（有账单周期与免息期）
    MORTGAGE = "mortgage"      # 按揭（实体店抵押、分期还款）
    DISCOUNT = "discount"      # 再贴现窗口（抵押融资）


class DepositStatus(str, Enum):
    ACTIVE = "active"          # 计息中
    MATURED = "matured"        # 已到期（定期转活期后置此态）
    WITHDRAWN = "withdrawn"    # 已结清取出
    SEIZED = "seized"          # 被追债抵债


class CreditStatus(str, Enum):
    ACTIVE = "active"          # 正常
    OVERDUE = "overdue"        # 逾期
    COLLECTING = "collecting"  # 已转追债
    CLOSED = "closed"          # 已结清/注销


class MortgageStatus(str, Enum):
    ACTIVE = "active"
    OVERDUE = "overdue"
    SETTLED = "settled"        # 已还清
    FORECLOSED = "foreclosed"  # 已收抵押物


class PolicyLock(str, Enum):
    """行长手动锁定的政策项 —— 锁定后自动调节会跳过它。"""

    BASE_RATE = "base_rate"
    RRR = "rrr"
    OMO = "omo"
    INFLATION_TARGET = "inflation_target"


class OmoSide(str, Enum):
    """公开市场操作的方向。买入投放货币，卖出回收货币。"""

    BUY = "buy"      # 央行花钱买物资 → 投放货币（抗通缩）
    SELL = "sell"    # 央行卖物资收钱 → 回收货币（抗通胀）


class ShopType(str, Enum):
    """商店类型。"""

    SELL = "sell"    # 出售：玩家花钱买东西
    BUY = "buy"      # 收购：玩家卖东西换钱
    BARTER = "barter"  # 以物易物
    BOTH = "both"    # 既可买又可卖（官方定价模式）


class PricingMode(str, Enum):
    MANUAL = "manual"      # 店主自定单价
    OFFICIAL = "official"  # 官方指导价 + 动态定价


class ShopStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    MANUAL = "manual"  # 事务异常，冻结中


class TradeDirection(str, Enum):
    """一次成交的方向（以玩家为主体）。"""

    BUY = "buy"    # 玩家买入（付出钱）
    SELL = "sell"  # 玩家卖出（收到钱）


class ItemFlow(str, Enum):
    """物品流向，用于背包副作用与 saga 回滚。"""

    GIVE = "give"    # 发给玩家
    TAKE = "take"    # 从玩家收取


# 出售/收购在商店类型下的映射
DIRECTION_BY_SHOP_TYPE = {
    ShopType.SELL.value: TradeDirection.BUY.value,
    ShopType.BUY.value: TradeDirection.SELL.value,
}

# 反向：玩家买入时商店是「出货」，玩家卖出时商店是「收货」
OPPOSITE_DIRECTION = {
    TradeDirection.BUY.value: TradeDirection.SELL.value,
    TradeDirection.SELL.value: TradeDirection.BUY.value,
}
