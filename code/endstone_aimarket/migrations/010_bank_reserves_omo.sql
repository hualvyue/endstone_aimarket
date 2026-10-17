-- 010 央行实物储备与公开市场操作
--
-- 公开市场操作是央行吞吐货币的主要手段：买入物资 = 印钞投放，卖出物资 = 回收销毁。
-- 它同时也是"汇率干预"（金对实物购买力）的执行器 —— 两者是同一次操作的两个视角：
--   * 公开市场操作看的是货币量与利率
--   * 汇率干预看的是物价与购买力（CPI）
--
-- 央行储备不入货币账本（实物不是货币），单独记在这张表里，用账面成本估值。

CREATE TABLE bank_reserves (
    item_key         TEXT PRIMARY KEY,
    item_name        TEXT,
    qty              INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    book_value_minor INTEGER NOT NULL DEFAULT 0,   -- 账面成本合计（按买入价累计）
    last_op_ms       INTEGER NOT NULL DEFAULT 0,
    updated_at_ms    INTEGER NOT NULL
);

-- 公开市场操作台账。每一笔都能对上"基础货币变化"与"储备变化"。
CREATE TABLE bank_omo_log (
    op_id           TEXT PRIMARY KEY,
    side            TEXT NOT NULL,                 -- buy / sell
    item_key        TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    unit_minor      INTEGER NOT NULL,              -- 实际成交单价
    amount_minor    INTEGER NOT NULL,              -- 成交总额
    price_bps       INTEGER NOT NULL DEFAULT 0,    -- 相对官方价的偏移
    is_qe           INTEGER NOT NULL DEFAULT 0,    -- 1 = 属于量化宽松/紧缩
    actor_account_id TEXT,
    ledger_tx_id    TEXT,
    m0_before_minor INTEGER NOT NULL DEFAULT 0,
    m0_after_minor  INTEGER NOT NULL DEFAULT 0,
    detail          TEXT,
    created_at_ms   INTEGER NOT NULL
);

CREATE INDEX ix_bank_omo_side ON bank_omo_log(side, created_at_ms);
CREATE INDEX ix_bank_omo_item ON bank_omo_log(item_key, created_at_ms);
