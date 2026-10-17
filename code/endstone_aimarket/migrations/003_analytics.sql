-- 003 指标：为将来的 AI / 运营分析准备的数据源
-- 一期只负责「采」不负责「算模型」，所以表结构刻意保持通用：
--   时间桶 + 粒度 + 指标名 + 维度 的四元组主键，后续加指标不需要改表。

CREATE TABLE metric_samples (
    bucket_ms    INTEGER NOT NULL,
    granularity  TEXT NOT NULL,
    metric       TEXT NOT NULL,
    dimension    TEXT NOT NULL,
    value_int    INTEGER,
    value_real   REAL,
    details_json TEXT,
    PRIMARY KEY (bucket_ms, granularity, metric, dimension)
);

CREATE INDEX ix_metric_samples_metric ON metric_samples(metric, granularity, bucket_ms);

-- 异常交易标记（自买自卖 / 高频 / 循环转账 / 金额离群）
CREATE TABLE risk_flags (
    flag_id      TEXT PRIMARY KEY,
    trade_id     TEXT,
    account_id   TEXT,
    item_key     TEXT,
    flag         TEXT NOT NULL,
    severity     INTEGER NOT NULL DEFAULT 1,
    details_json TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_risk_flags_account ON risk_flags(account_id, created_at_ms);
CREATE INDEX ix_risk_flags_trade ON risk_flags(trade_id);
