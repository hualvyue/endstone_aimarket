-- 008 公司募资：投资 / 融资 / 赞助
--
-- 三者都是「公司拿到钱」，区别在于出资方换到了什么：
--   投资 invest   → 股份（参与分红、承担股价波动）
--   融资 finance  → 债权（到期还本付息，不占股）
--   赞助 sponsor  → 广告位（公司名下实体店的详情页署名）
--
-- 「融资」刻意复用 loans 表而不是另建一张：债务的形态、计息、逾期、清偿
-- 与银行贷款完全一致，只是**债权人换成了玩家**。加两列就够，不必重复实现一遍。

-- 债权来源：NULL / 'bank' 表示系统银行放款，'player' 表示玩家出资
ALTER TABLE loans ADD COLUMN lender_account_id TEXT;
ALTER TABLE loans ADD COLUMN lender_kind TEXT NOT NULL DEFAULT 'bank';

-- 公司募资条款与防滥用状态
ALTER TABLE companies ADD COLUMN finance_rate_bps_per_day INTEGER NOT NULL DEFAULT 100;
ALTER TABLE companies ADD COLUMN finance_term_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE companies ADD COLUMN sponsor_price_per_day_minor INTEGER NOT NULL DEFAULT 0;
-- 募资来的钱在此时刻之前不能提现给发起人、也不能分红（防"募完就跑"）
ALTER TABLE companies ADD COLUMN fundraising_locked_until_ms INTEGER NOT NULL DEFAULT 0;
-- 上次募资时间，用于冷却
ALTER TABLE companies ADD COLUMN last_fundraise_ms INTEGER NOT NULL DEFAULT 0;
-- 累计募资金额，用于与公司资产挂钩的总量上限
ALTER TABLE companies ADD COLUMN total_raised_minor INTEGER NOT NULL DEFAULT 0;

CREATE INDEX ix_loans_lender ON loans(lender_account_id, status);

-- 赞助记录。广告的寿命由 expires_at_ms 决定，过期后店铺详情页不再署名。
CREATE TABLE company_sponsorships (
    sponsorship_id     TEXT PRIMARY KEY,
    company_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    sponsor_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    amount_minor       INTEGER NOT NULL CHECK (amount_minor > 0),
    display_name       TEXT,                       -- 署名（默认取赞助商当前名）
    starts_at_ms       INTEGER NOT NULL,
    expires_at_ms      INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',   -- active / expired / cancelled
    created_at_ms      INTEGER NOT NULL
);

CREATE INDEX ix_sponsor_company ON company_sponsorships(company_account_id, status, expires_at_ms);
CREATE INDEX ix_sponsor_sponsor ON company_sponsorships(sponsor_account_id, created_at_ms);

-- 募资流水：谁以什么方式给了这家公司多少钱。三种方式共用一张表便于统计与风控。
CREATE TABLE company_fundraising (
    record_id          TEXT PRIMARY KEY,
    company_account_id TEXT NOT NULL,
    funder_account_id  TEXT NOT NULL,
    kind               TEXT NOT NULL,              -- invest / finance / sponsor
    amount_minor       INTEGER NOT NULL,
    units              INTEGER NOT NULL DEFAULT 0, -- 投资=股数，赞助=天数
    ledger_tx_id       TEXT,
    created_at_ms      INTEGER NOT NULL
);

CREATE INDEX ix_fundraising_company ON company_fundraising(company_account_id, kind, created_at_ms);
CREATE INDEX ix_fundraising_funder ON company_fundraising(funder_account_id, created_at_ms);
