-- 009 中央银行：主体、政策、存款、授信、理财、监管
--
-- 设计要点：
--   * 央行 = 发行(sys:mint) + 财政(sys:treasury) + 实物储备，三家语义合一，
--     在 /bank 面板呈现为一张资产负债表。不物理搬钱 —— 账本分录里存着当时的
--     余额快照，搬钱会让历史快照对不上、审计立刻报警。
--   * 存款账户走与 escrow 相同的模式：每一笔存款对应一个 accounts 行（kind='deposit'）。
--     复式记账原生支持，M1 可精确统计，且 kind != 'system' 天然禁负。
--   * 透支不把余额改成负数（数据库有硬约束），而是"授信额度 + 自动放款"：
--     付款时余额不足就从额度里借一笔补上，玩家账户余额恒 >= 0。

-- ==================== 信贷产品接入 loans ====================
--
-- 透支 / 信用卡 / 按揭 / 再贴现**全部复用 loans 表**，只在 product 上区分。
-- 理由：计息、逾期判定、追债（没收背包+变卖）、坏账核销、清算受偿全都现成，
-- 另起炉灶等于把这些容易出错的逻辑重写一遍。产品特有的元数据（额度、账单、
-- 抵押物）放在各自的表里，通过 loan_id 指回来。
--
--   product = loan / overdraft / card / mortgage / discount
--
-- interest_start_ms 提供"免息期"：为 0 表示从放款时刻起算；
-- 信用卡把它设为账单还款日，于是免息期内不计息。
ALTER TABLE loans ADD COLUMN product TEXT NOT NULL DEFAULT 'loan';
ALTER TABLE loans ADD COLUMN interest_start_ms INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_loans_product ON loans(product, status);

-- ==================== 行长 ====================

-- config.bank.governor_names 是声明式名单（服主填游戏名），本表是落地态。
-- 只认 xuid：玩家改名不丢权限，离线也不影响判定。
CREATE TABLE bank_governors (
    account_id    TEXT PRIMARY KEY REFERENCES accounts(account_id),
    xuid          TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    granted_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    note          TEXT
);

-- ==================== 政策状态（单行） ====================

CREATE TABLE bank_policy_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    base_rate_bps_per_day INTEGER NOT NULL DEFAULT 30,     -- 基准利率（日，基点）
    rrr_bps               INTEGER NOT NULL DEFAULT 1000,   -- 法定存款准备金率
    discount_wedge_bps    INTEGER NOT NULL DEFAULT 50,     -- 再贴现率 = 基准 + 此加价
    demand_spread_bps     INTEGER NOT NULL DEFAULT -15,    -- 活期利差（可为负：活期利率低于基准）
    term_premium_bps      INTEGER NOT NULL DEFAULT 20,     -- 定期利差加成
    credit_spread_bps     INTEGER NOT NULL DEFAULT 20,     -- 贷款利差
    od_premium_bps        INTEGER NOT NULL DEFAULT 40,     -- 透支利差
    card_premium_bps      INTEGER NOT NULL DEFAULT 60,     -- 信用卡利差
    target_inflation_bps  INTEGER NOT NULL DEFAULT 200,    -- 通胀目标（基点，200 = 2%）
    target_band_bps       INTEGER NOT NULL DEFAULT 100,    -- 容忍带
    auto_policy           INTEGER NOT NULL DEFAULT 1,      -- 是否开启自动调节
    max_step_bps          INTEGER NOT NULL DEFAULT 10,     -- 单次调整步长上限
    adjust_cooldown_ms    INTEGER NOT NULL DEFAULT 3600000,-- 调整冷却
    omo_max_per_op_minor  INTEGER NOT NULL DEFAULT 0,      -- 单次公开市场操作上限（0 = 按 M0 比例）
    qe_cap_bps            INTEGER NOT NULL DEFAULT 2000,   -- QE 总量封顶（占 M0 比例）
    qe_used_minor         INTEGER NOT NULL DEFAULT 0,      -- 已用 QE 额度
    reserve_floor_car_bps INTEGER NOT NULL DEFAULT 1000,   -- 资产覆盖率红线（1000 = 10%）
    -- 行长手动锁定的政策项（逗号分隔的 PolicyLock 值）。锁定的项自动调节会跳过。
    locked_json           TEXT,
    last_adjust_ms        INTEGER NOT NULL DEFAULT 0,
    created_at_ms         INTEGER NOT NULL,
    updated_at_ms         INTEGER NOT NULL
);

-- ==================== 存款 ====================

-- 每一笔存款对应一个独立的 accounts 行（deposit_account_id），钱实际躺在那里。
--   kind = demand 活期 / time 定期 / wealth 理财
CREATE TABLE bank_deposits (
    deposit_id         TEXT PRIMARY KEY,
    holder_account_id  TEXT NOT NULL REFERENCES accounts(account_id),
    deposit_account_id TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
    kind               TEXT NOT NULL,
    principal_minor    INTEGER NOT NULL CHECK (principal_minor >= 0),
    rate_bps_per_day   INTEGER NOT NULL DEFAULT 0,
    locked_until_ms    INTEGER NOT NULL DEFAULT 0,
    accrued_minor      INTEGER NOT NULL DEFAULT 0,
    paid_interest_minor INTEGER NOT NULL DEFAULT 0,
    last_accrual_ms    INTEGER NOT NULL DEFAULT 0,
    product_id         TEXT,                       -- 理财产品 id（仅 wealth）
    status             TEXT NOT NULL DEFAULT 'active',
    version            INTEGER NOT NULL DEFAULT 0,
    created_at_ms      INTEGER NOT NULL,
    updated_at_ms      INTEGER NOT NULL
);

CREATE INDEX ix_bank_deposits_holder ON bank_deposits(holder_account_id, kind, status);
CREATE INDEX ix_bank_deposits_mature ON bank_deposits(kind, status, locked_until_ms);

-- 计息幂等：同一 (存款, 时间桶) 只计一次，重跑不双记。与 loan_accruals 同法。
CREATE TABLE bank_interest_accruals (
    deposit_id     TEXT NOT NULL REFERENCES bank_deposits(deposit_id) ON DELETE CASCADE,
    bucket_ms      INTEGER NOT NULL,
    interest_minor INTEGER NOT NULL DEFAULT 0,
    hours          INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (deposit_id, bucket_ms)
);

-- ==================== 透支（授信额度 + 自动放款） ====================

CREATE TABLE bank_credit_lines (
    line_id             TEXT PRIMARY KEY,
    holder_account_id   TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
    credit_limit_minor  INTEGER NOT NULL DEFAULT 0,
    used_minor          INTEGER NOT NULL DEFAULT 0 CHECK (used_minor >= 0),
    rate_bps_per_day    INTEGER NOT NULL DEFAULT 0,
    due_at_ms           INTEGER NOT NULL DEFAULT 0,
    autopay             INTEGER NOT NULL DEFAULT 1,   -- 有余额时自动还款
    status              TEXT NOT NULL DEFAULT 'active',
    version             INTEGER NOT NULL DEFAULT 0,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

CREATE INDEX ix_bank_credit_status ON bank_credit_lines(status, due_at_ms);

-- ==================== 信用卡 ====================

-- 与"透支"的区别：透支是账户级、随借随还、无免息期、额度小；
-- 信用卡是独立卡、有账单周期与免息期、最低还款额、未还部分滚动计息 + 逾期罚息。
CREATE TABLE bank_cards (
    card_id            TEXT PRIMARY KEY,
    holder_account_id  TEXT NOT NULL UNIQUE REFERENCES accounts(account_id),
    credit_limit_minor INTEGER NOT NULL CHECK (credit_limit_minor > 0),
    used_minor         INTEGER NOT NULL DEFAULT 0 CHECK (used_minor >= 0),
    apr_bps_per_day    INTEGER NOT NULL DEFAULT 0,     -- 正常利息
    penalty_bps_per_day INTEGER NOT NULL DEFAULT 0,    -- 逾期罚息
    cycle_days         INTEGER NOT NULL DEFAULT 7,
    min_pay_bps        INTEGER NOT NULL DEFAULT 1000,  -- 最低还款比例
    grace_days         INTEGER NOT NULL DEFAULT 0,     -- 免息天数
    next_statement_ms  INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    version            INTEGER NOT NULL DEFAULT 0,
    created_at_ms      INTEGER NOT NULL,
    updated_at_ms      INTEGER NOT NULL
);

CREATE INDEX ix_bank_cards_holder ON bank_cards(holder_account_id, status);
CREATE INDEX ix_bank_cards_cycle ON bank_cards(status, next_statement_ms);

CREATE TABLE bank_card_statements (
    statement_id   TEXT PRIMARY KEY,
    card_id        TEXT NOT NULL REFERENCES bank_cards(card_id) ON DELETE CASCADE,
    period_end_ms  INTEGER NOT NULL,
    due_at_ms      INTEGER NOT NULL,
    amount_minor   INTEGER NOT NULL DEFAULT 0,     -- 本期账单金额
    min_pay_minor  INTEGER NOT NULL DEFAULT 0,
    paid_minor     INTEGER NOT NULL DEFAULT 0,
    penalty_minor  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',   -- open / paid / overdue / rolled
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
);

CREATE INDEX ix_bank_stmt_card ON bank_card_statements(card_id, status, due_at_ms);

-- ==================== 按揭（实体店抵押、分期还款） ====================

CREATE TABLE bank_mortgages (
    mortgage_id           TEXT PRIMARY KEY,
    borrower_account_id   TEXT NOT NULL REFERENCES accounts(account_id),
    loan_id               TEXT,                    -- 关联 loans 表（复用计息与追债）
    collateral_kind       TEXT NOT NULL,           -- shop / deposit / cash
    collateral_ref        TEXT NOT NULL,           -- 抵押物标识（如 shop_id）；唯一防重复抵押
    collateral_value_minor INTEGER NOT NULL,
    principal_minor       INTEGER NOT NULL CHECK (principal_minor >= 0),
    outstanding_minor     INTEGER NOT NULL DEFAULT 0,
    rate_bps_per_day      INTEGER NOT NULL DEFAULT 0,
    term_days             INTEGER NOT NULL,
    periods               INTEGER NOT NULL DEFAULT 1,
    next_due_ms           INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'active',
    version               INTEGER NOT NULL DEFAULT 0,
    created_at_ms         INTEGER NOT NULL,
    updated_at_ms         INTEGER NOT NULL
);

-- 同一抵押物只能抵押给一家（防重复抵押刷钱）
CREATE UNIQUE INDEX ux_bank_mortgage_collateral
    ON bank_mortgages(collateral_ref) WHERE status IN ('active','overdue');
CREATE INDEX ix_bank_mortgage_borrower ON bank_mortgages(borrower_account_id, status);

CREATE TABLE bank_mortgage_schedules (
    mortgage_id       TEXT NOT NULL REFERENCES bank_mortgages(mortgage_id) ON DELETE CASCADE,
    period_index      INTEGER NOT NULL,
    due_at_ms         INTEGER NOT NULL,
    principal_minor   INTEGER NOT NULL DEFAULT 0,
    interest_minor    INTEGER NOT NULL DEFAULT 0,
    paid_minor        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (mortgage_id, period_index)
);

-- ==================== 理财（本轮只落端口） ====================

-- strategy_key 指向一个 WealthStrategy 实现。本轮只有内置的低风险策略，
-- 后期接 AI 时注册一个新实现即可，界面与表都不用动。
CREATE TABLE bank_wealth_products (
    product_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    strategy_key     TEXT NOT NULL DEFAULT 'fixed',
    risk_level       INTEGER NOT NULL DEFAULT 1,   -- 1 低 / 2 中 / 3 高
    min_amount_minor INTEGER NOT NULL DEFAULT 0,
    term_days        INTEGER NOT NULL DEFAULT 7,
    expected_bps     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at_ms    INTEGER NOT NULL
);

CREATE TABLE bank_wealth_holdings (
    holding_id        TEXT PRIMARY KEY,
    product_id        TEXT NOT NULL REFERENCES bank_wealth_products(product_id),
    holder_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    deposit_id        TEXT,                        -- 底层资金账户（走同一套存款记账）
    amount_minor      INTEGER NOT NULL CHECK (amount_minor >= 0),
    settled_minor     INTEGER NOT NULL DEFAULT 0,
    return_bps        INTEGER NOT NULL DEFAULT 0,  -- 实际结算收益率
    matures_at_ms     INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active',
    created_at_ms     INTEGER NOT NULL,
    updated_at_ms     INTEGER NOT NULL
);

CREATE INDEX ix_bank_wealth_holder ON bank_wealth_holdings(holder_account_id, status);

-- ==================== 监管 ====================

CREATE TABLE bank_alerts (
    alert_id       TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,              -- run / car / inflation / big_loan / reserve
    severity       TEXT NOT NULL DEFAULT 'warning',
    title          TEXT NOT NULL,
    detail         TEXT,
    metrics_json   TEXT,
    acknowledged   INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL
);

CREATE INDEX ix_bank_alerts_kind ON bank_alerts(kind, acknowledged, created_at_ms);

-- 坏账核销：缺口直接减少央行资本（资产端减记、负债不变）
CREATE TABLE bank_writeoffs (
    writeoff_id        TEXT PRIMARY KEY,
    loan_id            TEXT,
    borrower_account_id TEXT,
    amount_minor       INTEGER NOT NULL,
    reason             TEXT,
    created_at_ms      INTEGER NOT NULL
);

-- 央行事件流水：挤兑、QE、政策调整、准备金变动等
CREATE TABLE bank_events (
    event_id      TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    amount_minor  INTEGER NOT NULL DEFAULT 0,
    m0_after_minor INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_bank_events_kind ON bank_events(kind, created_at_ms);
