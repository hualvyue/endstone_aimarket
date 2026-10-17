-- 001 核心：账户与复式账本
-- 设计要点：
--   * account_id 是唯一主键（UUIDv7），xuid 只作登录凭据 —— 玩家改名不换账户，不会丢钱
--   * balance_minor 是可用余额，held_minor 是被交易冻结的部分，恒有 held <= balance
--   * 账本采用复式记账：一个 ledger_tx 对应若干 ledger_entries，SUM(delta_minor) 必须 = 0
--   * idempotency_key 唯一约束 —— 天然幂等，玩家连点两次只生效一次

CREATE TABLE accounts (
    account_id       TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    xuid             TEXT UNIQUE,
    player_uuid      TEXT UNIQUE,
    current_name     TEXT,
    -- 只有 system 账户允许透支：它代表货币发行方，其余账户不允许出现负余额
    balance_minor    INTEGER NOT NULL DEFAULT 0
                     CHECK (balance_minor >= 0 OR kind = 'system'),
    held_minor       INTEGER NOT NULL DEFAULT 0 CHECK (held_minor >= 0),
    status           TEXT NOT NULL DEFAULT 'active',
    version          INTEGER NOT NULL DEFAULT 0,
    created_at_ms    INTEGER NOT NULL,
    updated_at_ms    INTEGER NOT NULL
);

CREATE TABLE account_names (
    account_id    TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    name_norm     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    is_current    INTEGER NOT NULL DEFAULT 0,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    PRIMARY KEY (account_id, name_norm)
);

CREATE UNIQUE INDEX ux_account_names_current
    ON account_names(name_norm) WHERE is_current = 1;

CREATE TABLE ledger_tx (
    tx_id              TEXT PRIMARY KEY,
    idempotency_key    TEXT NOT NULL UNIQUE,
    kind               TEXT NOT NULL,
    status             TEXT NOT NULL,
    supply_delta_minor INTEGER NOT NULL DEFAULT 0,
    correlation_id     TEXT,
    actor_account_id   TEXT,
    metadata_json      TEXT,
    created_at_ms      INTEGER NOT NULL,
    committed_at_ms    INTEGER
);

CREATE TABLE ledger_entries (
    entry_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id               TEXT NOT NULL REFERENCES ledger_tx(tx_id) ON DELETE CASCADE,
    account_id          TEXT NOT NULL REFERENCES accounts(account_id),
    role                TEXT NOT NULL,
    delta_minor         INTEGER NOT NULL,
    balance_after_minor INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    UNIQUE (tx_id, account_id, role)
);

CREATE INDEX ix_ledger_entries_account ON ledger_entries(account_id, entry_id);
CREATE INDEX ix_ledger_tx_created ON ledger_tx(created_at_ms);
