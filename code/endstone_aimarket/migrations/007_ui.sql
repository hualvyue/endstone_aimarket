-- 007 玩家自选显示
--
-- 玩家自己决定要在侧边栏/行动栏上看到什么（余额、欠款、某支股票的价格……）。
-- items_json 里只存「显示项的名字列表」，具体内容由 DisplayRegistry 里的
-- provider 在渲染时实时取 —— 加新指标只需注册一个 provider，界面零改动。

CREATE TABLE player_ui_prefs (
    player_account_id TEXT PRIMARY KEY REFERENCES accounts(account_id) ON DELETE CASCADE,
    carrier           TEXT NOT NULL DEFAULT 'sidebar',   -- sidebar / actionbar / bossbar / nametag
    items_json        TEXT NOT NULL DEFAULT '[]',
    sort_mode         TEXT NOT NULL DEFAULT 'custom',
    refresh_ticks     INTEGER NOT NULL DEFAULT 40,        -- 默认 2 秒刷新一次
    enabled           INTEGER NOT NULL DEFAULT 1,
    updated_at_ms     INTEGER NOT NULL
);

-- 显示项注册表（运行时由代码注册，这里只留一张表方便服主覆盖显示名与顺序）
CREATE TABLE display_item_catalog (
    item_key      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    description   TEXT,
    default_on    INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 100
);
