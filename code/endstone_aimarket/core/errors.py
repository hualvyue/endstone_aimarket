"""统一错误码。

业务失败一律返回 Result.failure(code)，不抛异常；
只有「参数非法」和「数据库损坏」才允许抛异常。
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    OK = "ok"

    # --- 账户 ---
    ACCOUNT_NOT_FOUND = "account.not_found"
    ACCOUNT_EXISTS = "account.exists"
    ACCOUNT_FROZEN = "account.frozen"
    ACCOUNT_CLOSED = "account.closed"
    AMBIGUOUS_NAME = "account.ambiguous_name"

    # --- 资金 ---
    INSUFFICIENT_FUNDS = "funds.insufficient"
    INVALID_AMOUNT = "funds.invalid_amount"
    SUPPLY_LIMIT = "funds.supply_limit"
    BALANCE_DRIFT = "funds.balance_drift"

    # --- 交易 ---
    DUPLICATE_TX = "tx.duplicate"
    TX_NOT_FOUND = "tx.not_found"
    TX_BUSY = "tx.busy"
    TX_EXPIRED = "tx.expired"
    TX_STATE = "tx.bad_state"
    ENTRY_UNBALANCED = "tx.entry_unbalanced"

    # --- 商店 ---
    SHOP_NOT_FOUND = "shop.not_found"
    SHOP_OCCUPIED = "shop.occupied"
    SHOP_NOT_ACTIVE = "shop.not_active"
    SHOP_LIMIT = "shop.limit_reached"
    SHOP_FORBIDDEN = "shop.forbidden"
    SHOP_SOLD_OUT = "shop.sold_out"
    SHOP_NO_BUDGET = "shop.no_budget"
    SHOP_BUY_SUSPENDED = "shop.buy_suspended"
    SHOP_BAD_CONFIG = "shop.bad_config"

    # --- 背包 ---
    INVENTORY_FULL = "inventory.full"
    ITEM_NOT_ENOUGH = "inventory.item_not_enough"
    ITEM_REMOVE_FAILED = "inventory.remove_failed"
    ITEM_GIVE_FAILED = "inventory.give_failed"

    # --- 定价 ---
    PRICE_NOT_SET = "price.not_set"
    PRICE_STALE = "price.stale"

    # --- 中央银行 ---
    BANK_DISABLED = "bank.disabled"
    BANK_NOT_GOVERNOR = "bank.not_governor"          # 不是央行行长
    BANK_NO_GOVERNOR = "bank.no_governor"            # config 里还没填行长名字
    BANK_RESERVE_LIMIT = "bank.reserve_limit"        # 准备金闸门：超出可放贷上限
    BANK_CAR_LOW = "bank.car_low"                    # 资本充足率跌破红线，已停贷
    BANK_DEPOSIT_NOT_FOUND = "bank.deposit_not_found"
    BANK_DEPOSIT_LOCKED = "bank.deposit_locked"      # 定期未到期
    BANK_CREDIT_LIMIT = "bank.credit_limit"          # 超出授信额度
    BANK_CARD_MISSING = "bank.card_missing"
    BANK_MORTGAGE_INVALID = "bank.mortgage_invalid"  # 抵押物不合格/已抵押
    BANK_RESERVE_EMPTY = "bank.reserve_empty"        # 央行储备不足，无法卖出
    BANK_NEED_CONFIRM = "bank.need_confirm"          # 大额操作需二次确认
    BANK_COOLDOWN = "bank.cooldown"                  # 操作冷却中

    # --- 通用 ---
    NOT_PLAYER = "common.not_player"
    PERMISSION_DENIED = "common.permission_denied"
    STORAGE_ERROR = "common.storage_error"
    INTERNAL = "common.internal"


class AimarketError(Exception):
    """真正的异常：参数非法 / 存储损坏。业务失败请勿使用。"""

    def __init__(self, code: ErrorCode, message: str = ""):
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value


# 错误码 -> 语言键
CODE_LANG_KEY = {
    ErrorCode.ACCOUNT_NOT_FOUND: "err.account_not_found",
    ErrorCode.ACCOUNT_EXISTS: "err.account_exists",
    ErrorCode.ACCOUNT_FROZEN: "err.account_frozen",
    ErrorCode.ACCOUNT_CLOSED: "err.account_closed",
    ErrorCode.AMBIGUOUS_NAME: "err.ambiguous_name",
    ErrorCode.INSUFFICIENT_FUNDS: "err.insufficient_funds",
    ErrorCode.INVALID_AMOUNT: "err.invalid_amount",
    ErrorCode.SUPPLY_LIMIT: "err.supply_limit",
    ErrorCode.DUPLICATE_TX: "err.duplicate_tx",
    ErrorCode.TX_BUSY: "err.tx_busy",
    ErrorCode.TX_EXPIRED: "err.tx_expired",
    ErrorCode.SHOP_NOT_FOUND: "err.shop_not_found",
    ErrorCode.SHOP_OCCUPIED: "err.shop_occupied",
    ErrorCode.SHOP_NOT_ACTIVE: "err.shop_not_active",
    ErrorCode.SHOP_LIMIT: "err.shop_limit",
    ErrorCode.SHOP_FORBIDDEN: "err.shop_forbidden",
    ErrorCode.SHOP_SOLD_OUT: "err.shop_sold_out",
    ErrorCode.SHOP_NO_BUDGET: "err.shop_no_budget",
    ErrorCode.SHOP_BUY_SUSPENDED: "err.shop_buy_suspended",
    ErrorCode.SHOP_BAD_CONFIG: "err.shop_bad_config",
    ErrorCode.INVENTORY_FULL: "err.inventory_full",
    ErrorCode.ITEM_NOT_ENOUGH: "err.item_not_enough",
    ErrorCode.ITEM_REMOVE_FAILED: "err.item_remove_failed",
    ErrorCode.ITEM_GIVE_FAILED: "err.item_give_failed",
    ErrorCode.PRICE_NOT_SET: "err.price_not_set",
    ErrorCode.PRICE_STALE: "err.price_stale",
    ErrorCode.BANK_DISABLED: "err.bank_disabled",
    ErrorCode.BANK_NOT_GOVERNOR: "err.bank_not_governor",
    ErrorCode.BANK_NO_GOVERNOR: "err.bank_no_governor",
    ErrorCode.BANK_RESERVE_LIMIT: "err.bank_reserve_limit",
    ErrorCode.BANK_CAR_LOW: "err.bank_car_low",
    ErrorCode.BANK_DEPOSIT_NOT_FOUND: "err.bank_deposit_not_found",
    ErrorCode.BANK_DEPOSIT_LOCKED: "err.bank_deposit_locked",
    ErrorCode.BANK_CREDIT_LIMIT: "err.bank_credit_limit",
    ErrorCode.BANK_CARD_MISSING: "err.bank_card_missing",
    ErrorCode.BANK_MORTGAGE_INVALID: "err.bank_mortgage_invalid",
    ErrorCode.BANK_RESERVE_EMPTY: "err.bank_reserve_empty",
    ErrorCode.BANK_NEED_CONFIRM: "err.bank_need_confirm",
    ErrorCode.BANK_COOLDOWN: "err.bank_cooldown",
    ErrorCode.NOT_PLAYER: "err.not_player",
    ErrorCode.PERMISSION_DENIED: "err.permission_denied",
    ErrorCode.STORAGE_ERROR: "err.storage_error",
    ErrorCode.INTERNAL: "err.internal",
}

DEFAULT_MESSAGES = {
    ErrorCode.INSUFFICIENT_FUNDS: "余额不足",
    ErrorCode.INVENTORY_FULL: "背包已满",
    ErrorCode.SHOP_SOLD_OUT: "库存不足",
    ErrorCode.ITEM_NOT_ENOUGH: "没有足够的物品",
}
