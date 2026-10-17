-- 外部货币桥接：待同步队列。
-- 每次玩家账户余额变动都会在同事务内记录"提交后应推送的外部余额"，
-- 推送成功后删除。崩溃/推送失败由定时任务与上线对账兜底，保证最终一致。
CREATE TABLE IF NOT EXISTS bridge_pending (
    account_id     TEXT PRIMARY KEY,
    pending_minor  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL
);
