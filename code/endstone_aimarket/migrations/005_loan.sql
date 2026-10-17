-- 005 贷款与追债
--
-- 资金来自系统增发：放贷 = 系统账户 -> 借款人（mint），
-- 还款 = 借款人 -> 系统账户（burn）+ 利息进国库。所以"坏账"天然体现为
-- 系统账户透支额增加，也就是已投放货币增加，会被 M0 指标如实反映出来。
--
-- 关于 guarantee_account_id 上的那条 CHECK：
-- 它禁止把「别人的账户」设为担保 —— 担保方只能是借款人自己（等效于不允许第三方账户担保）。
-- 结合 accounts 的禁负约束，公司的债务在结构上无法穿透到股东的个人账户。

CREATE TABLE loans (
    loan_id                     TEXT PRIMARY KEY,
    borrower_account_id         TEXT NOT NULL REFERENCES accounts(account_id),
    is_company                  INTEGER NOT NULL DEFAULT 0,
    principal_minor             INTEGER NOT NULL CHECK (principal_minor > 0),
    outstanding_principal_minor INTEGER NOT NULL CHECK (outstanding_principal_minor >= 0),
    accrued_interest_minor      INTEGER NOT NULL DEFAULT 0 CHECK (accrued_interest_minor >= 0),
    paid_interest_minor         INTEGER NOT NULL DEFAULT 0,
    rate_bps_per_day            INTEGER NOT NULL CHECK (rate_bps_per_day >= 0),
    term_days                   INTEGER NOT NULL CHECK (term_days > 0),
    disbursed_at_ms             INTEGER NOT NULL,
    due_at_ms                   INTEGER NOT NULL,
    grace_until_ms              INTEGER NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active',
    guarantee_account_id        TEXT,
    credit_limit_minor          INTEGER NOT NULL DEFAULT 0,
    last_accrual_ms             INTEGER NOT NULL DEFAULT 0,
    reason                      TEXT,
    version                     INTEGER NOT NULL DEFAULT 0,
    created_at_ms               INTEGER NOT NULL,
    updated_at_ms               INTEGER NOT NULL,
    CHECK (guarantee_account_id IS NULL OR guarantee_account_id = borrower_account_id)
);

CREATE INDEX ix_loans_borrower ON loans(borrower_account_id, status);
CREATE INDEX ix_loans_status ON loans(status, due_at_ms);

CREATE TABLE loan_repayments (
    repay_id            TEXT PRIMARY KEY,
    loan_id             TEXT NOT NULL REFERENCES loans(loan_id) ON DELETE CASCADE,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    principal_part_minor INTEGER NOT NULL DEFAULT 0,
    interest_part_minor  INTEGER NOT NULL DEFAULT 0,
    ledger_tx_id        TEXT,
    source              TEXT NOT NULL DEFAULT 'manual',  -- manual / seize / liquidate / auto
    paid_at_ms          INTEGER NOT NULL
);

CREATE INDEX ix_loan_repayments_loan ON loan_repayments(loan_id, paid_at_ms);

-- 按小时计提利息。(loan_id, bucket_ms) 作为主键 → 重复计提天然幂等。
CREATE TABLE loan_accruals (
    loan_id             TEXT NOT NULL REFERENCES loans(loan_id) ON DELETE CASCADE,
    bucket_ms           INTEGER NOT NULL,
    interest_delta_minor INTEGER NOT NULL DEFAULT 0,
    created_at_ms       INTEGER NOT NULL,
    PRIMARY KEY (loan_id, bucket_ms)
);

CREATE TABLE loan_defaults (
    default_id     TEXT PRIMARY KEY,
    loan_id        TEXT NOT NULL REFERENCES loans(loan_id) ON DELETE CASCADE,
    reason         TEXT NOT NULL,
    trigger_kind   TEXT,               -- overdue / over_limit
    snapshot_json  TEXT,
    created_at_ms  INTEGER NOT NULL
);

CREATE INDEX ix_loan_defaults_loan ON loan_defaults(loan_id);

-- 被没收的物品。**追债流程的关键**：背包不可回滚，所以先把"拿到了什么"落库，
-- 再去变卖结算。即使中途崩溃，物品也在这里躺着，能重新结算，绝不吞物品。
CREATE TABLE repossessed_items (
    item_uid        TEXT PRIMARY KEY,
    loan_id         TEXT NOT NULL REFERENCES loans(loan_id) ON DELETE CASCADE,
    item_json       TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    est_value_minor INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'stored',   -- stored / sold / returned
    sold_price_minor INTEGER,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE INDEX ix_repossessed_loan ON repossessed_items(loan_id, status);
