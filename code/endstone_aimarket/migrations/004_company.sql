-- 004 公司
-- 核心设计：公司是一个独立的账户主体（accounts.kind = 'company'）。
-- accounts 表已有约束 CHECK (balance_minor >= 0 OR kind = 'system')，
-- 公司不属于 system，因此**公司账户在数据库层面永远不可能为负**。
-- 债务无法被记到公司账上，缺口只能由系统账户核销 ——
-- 「公司欠债不牵连股东」不是靠代码自觉，而是有这个物理约束兜底。

CREATE TABLE companies (
    business_id            TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
    name                   TEXT NOT NULL,
    name_norm              TEXT NOT NULL UNIQUE,     -- 小写归一，保证名字不区分大小写唯一
    founder_account_id     TEXT NOT NULL REFERENCES accounts(account_id),
    registered_capital_minor INTEGER NOT NULL CHECK (registered_capital_minor >= 0),
    turnover_tax_bps       INTEGER NOT NULL DEFAULT 0 CHECK (turnover_tax_bps >= 0),
    dividend_bps           INTEGER NOT NULL DEFAULT 0,   -- 分红率：利润拿出多少分给股东
    reserve_minor          INTEGER NOT NULL DEFAULT 0,   -- 必须保留的运营金，不参与分红
    status                 TEXT NOT NULL DEFAULT 'active',
    created_at_ms          INTEGER NOT NULL,
    updated_at_ms          INTEGER NOT NULL
);

CREATE INDEX ix_companies_status ON companies(status);
CREATE INDEX ix_companies_founder ON companies(founder_account_id);

-- 成员表。role=founder 是法人代表（唯一有权代表公司操作的人），
-- 其余是普通股东（只能查看与分红）。这样避免"谁都能动公司的钱"。
CREATE TABLE company_members (
    business_id   TEXT NOT NULL REFERENCES companies(business_id) ON DELETE CASCADE,
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    role          TEXT NOT NULL DEFAULT 'shareholder',
    joined_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (business_id, account_id)
);

CREATE INDEX ix_company_members_account ON company_members(account_id);

-- 公司经营流水（营业额口径，用于税务与 NAV 分析）。
-- 注意：真正的扣税发生在交易确认事务内，这张表只作审计与统计用途。
CREATE TABLE company_turnover (
    entry_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id    TEXT NOT NULL REFERENCES companies(business_id) ON DELETE CASCADE,
    shop_id        TEXT,
    direction      TEXT,
    gross_minor    INTEGER NOT NULL DEFAULT 0,
    tax_minor      INTEGER NOT NULL DEFAULT 0,
    occurred_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_company_turnover_biz ON company_turnover(business_id, occurred_at_ms);
