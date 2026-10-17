-- 002 市场：定价状态、商店、成交、背包副作用补偿记录
-- 设计要点：
--   * 收购店的「预算」不使用独立字段，而是托管账户(escrow)的余额 ——
--     这样预算与资金本身同一份数据，天然享受事务保护，不会出现"账上有钱但预算没扣"的错位
--   * shop_trades 记录最终成交，market_history 只在成交事务内写入 ——
--     原代码在扣款前就记交易量，导致失败的交易也永久污染物价，这里从结构上杜绝
--   * inventory_sagas 保存背包副作用镜像，用于崩溃后判断该补确认还是补偿

CREATE TABLE price_state (
    item_key        TEXT PRIMARY KEY,
    base_buy_minor  INTEGER NOT NULL,
    base_sell_minor INTEGER NOT NULL,
    demand_buy_bps  INTEGER NOT NULL DEFAULT 0,
    demand_sell_bps INTEGER NOT NULL DEFAULT 0,
    daily_bps       INTEGER NOT NULL DEFAULT 0,
    buy_acc_minor   INTEGER NOT NULL DEFAULT 0,
    sell_acc_minor  INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE market_history (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    side            TEXT,
    quantity        INTEGER NOT NULL DEFAULT 0,
    gross_minor     INTEGER NOT NULL DEFAULT 0,
    unit_minor      INTEGER,
    source_trade_id TEXT,
    occurred_at_ms  INTEGER NOT NULL
);

CREATE INDEX ix_market_history_item_time ON market_history(item_key, occurred_at_ms);
CREATE INDEX ix_market_history_time ON market_history(occurred_at_ms);

CREATE TABLE shops (
    shop_id            TEXT PRIMARY KEY,
    owner_account_id   TEXT NOT NULL REFERENCES accounts(account_id),
    escrow_account_id  TEXT REFERENCES accounts(account_id),
    shop_type          TEXT NOT NULL,
    pricing_mode       TEXT NOT NULL DEFAULT 'manual',
    dimension          TEXT NOT NULL,
    x                  INTEGER NOT NULL,
    y                  INTEGER NOT NULL,
    z                  INTEGER NOT NULL,
    chunk_x            INTEGER NOT NULL,
    chunk_z            INTEGER NOT NULL,
    offer_item_json    TEXT NOT NULL,
    cost_item_json     TEXT,
    offer_per_lot      INTEGER NOT NULL DEFAULT 1,
    cost_per_lot       INTEGER NOT NULL DEFAULT 1,
    manual_buy_unit_minor  INTEGER,
    manual_sell_unit_minor INTEGER,
    discount_bps       INTEGER NOT NULL DEFAULT 0,
    stock_qty          INTEGER NOT NULL DEFAULT 0,
    held_stock_qty     INTEGER NOT NULL DEFAULT 0,
    collected_json     TEXT,
    is_infinite        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    version            INTEGER NOT NULL DEFAULT 0,
    price_version      INTEGER NOT NULL DEFAULT 0,
    created_at_ms      INTEGER NOT NULL,
    updated_at_ms      INTEGER NOT NULL,
    UNIQUE (dimension, x, y, z)
);

CREATE INDEX ix_shops_owner ON shops(owner_account_id);
CREATE INDEX ix_shops_chunk ON shops(dimension, chunk_x, chunk_z);

CREATE TABLE shop_trades (
    trade_id         TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    shop_id          TEXT NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    actor_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    direction        TEXT NOT NULL,
    status           TEXT NOT NULL,
    requested_qty    INTEGER NOT NULL,
    filled_qty       INTEGER NOT NULL DEFAULT 0,
    unit_minor       INTEGER NOT NULL DEFAULT 0,
    gross_minor      INTEGER NOT NULL DEFAULT 0,
    tax_bps          INTEGER NOT NULL DEFAULT 0,
    tax_minor        INTEGER NOT NULL DEFAULT 0,
    net_minor        INTEGER NOT NULL DEFAULT 0,
    frozen_minor     INTEGER NOT NULL DEFAULT 0,
    frozen_stock     INTEGER NOT NULL DEFAULT 0,
    price_version    INTEGER,
    ledger_tx_id     TEXT,
    risk_flags_json  TEXT,
    created_at_ms    INTEGER NOT NULL,
    finished_at_ms   INTEGER
);

CREATE INDEX ix_shop_trades_shop ON shop_trades(shop_id, created_at_ms);
CREATE INDEX ix_shop_trades_actor ON shop_trades(actor_account_id, created_at_ms);
CREATE INDEX ix_shop_trades_status ON shop_trades(status);

CREATE TABLE inventory_sagas (
    trade_id      TEXT PRIMARY KEY REFERENCES shop_trades(trade_id) ON DELETE CASCADE,
    xuid          TEXT NOT NULL,
    player_name   TEXT,
    phase         TEXT NOT NULL,
    effects_json  TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
