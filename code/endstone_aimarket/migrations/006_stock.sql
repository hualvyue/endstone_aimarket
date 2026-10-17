-- 006 股票（系统做市）
--
-- 定价模型：股价 = 每股真实资产 × (1 ± 随机指数)，再套涨跌停。
-- 每股真实资产 = 公司 NAV / 流通股，NAV 见 company 模块的估值口径。
--
-- 关键的防稀释约束：申购时「钱进公司账户」与「流通股增加」必须同事务提交。
-- 现金与股本同比例增加，每股净资产基本不变，老股东不被稀释。
-- 这两步若分开提交，就等于拿老股东的钱补贴新股东。

CREATE TABLE company_shares (
    company_account_id   TEXT PRIMARY KEY REFERENCES accounts(account_id),
    authorized_shares    INTEGER NOT NULL CHECK (authorized_shares > 0),  -- 授权股本上限
    circulating_shares   INTEGER NOT NULL CHECK (circulating_shares >= 0),
    ipo_price_minor      INTEGER NOT NULL CHECK (ipo_price_minor > 0),
    last_price_minor     INTEGER NOT NULL CHECK (last_price_minor >= 0),
    nav_minor            INTEGER NOT NULL DEFAULT 0,
    nav_per_share_minor  INTEGER NOT NULL DEFAULT 0,
    rand_bps             INTEGER NOT NULL DEFAULT 0,     -- 本次定价用的随机指数，可审计
    price_version        INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'listed',
    listed_at_ms         INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL
);

CREATE INDEX ix_shares_status ON company_shares(status, last_price_minor);

CREATE TABLE share_holdings (
    company_account_id TEXT NOT NULL REFERENCES company_shares(company_account_id) ON DELETE CASCADE,
    holder_account_id  TEXT NOT NULL REFERENCES accounts(account_id),
    shares             INTEGER NOT NULL CHECK (shares >= 0),
    avg_cost_minor     INTEGER NOT NULL DEFAULT 0,
    updated_at_ms      INTEGER NOT NULL,
    PRIMARY KEY (company_account_id, holder_account_id)
);

CREATE INDEX ix_holdings_holder ON share_holdings(holder_account_id);

-- 价格历史：每个定价周期一条，含当时的 NAV 与随机指数，便于事后复盘与风控。
CREATE TABLE share_price_history (
    company_account_id    TEXT NOT NULL,
    bucket_ms             INTEGER NOT NULL,
    nav_minor             INTEGER NOT NULL,
    circulating_shares    INTEGER NOT NULL,
    nav_per_share_minor   INTEGER NOT NULL,
    rand_bps              INTEGER NOT NULL,
    price_minor           INTEGER NOT NULL,
    PRIMARY KEY (company_account_id, bucket_ms)
);

-- 分红记录。idem 唯一 → 重复触发不会双发。
CREATE TABLE dividend_records (
    dividend_id        TEXT PRIMARY KEY,
    company_account_id TEXT NOT NULL,
    per_share_minor    INTEGER NOT NULL,
    total_minor        INTEGER NOT NULL,
    shareholder_count  INTEGER NOT NULL DEFAULT 0,
    tax_minor          INTEGER NOT NULL DEFAULT 0,
    idem               TEXT NOT NULL UNIQUE,
    paid_at_ms         INTEGER NOT NULL
);

CREATE INDEX ix_dividend_company ON dividend_records(company_account_id, paid_at_ms);

-- 申购/赎回流水，便于对账与展示
CREATE TABLE share_orders (
    order_id           TEXT PRIMARY KEY,
    company_account_id TEXT NOT NULL,
    holder_account_id  TEXT NOT NULL REFERENCES accounts(account_id),
    side               TEXT NOT NULL,          -- issue / redeem
    shares             INTEGER NOT NULL,
    unit_price_minor   INTEGER NOT NULL,
    amount_minor       INTEGER NOT NULL,
    ledger_tx_id       TEXT,
    created_at_ms      INTEGER NOT NULL
);

CREATE INDEX ix_share_orders_holder ON share_orders(holder_account_id, created_at_ms);
CREATE INDEX ix_share_orders_company ON share_orders(company_account_id, created_at_ms);
