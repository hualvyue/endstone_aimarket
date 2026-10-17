-- 011 拍卖系统：现场拍 / 离线拍
-- 设计要点：
--   * 每场拍卖一个独立的 escrow 托管账户，当前最高出价者的钱冻结在里面；
--     被超过时原路退回，结算时从托管账户划给卖家，与商店托管同口径（不计入 M0）。
--   * 物品在创建时即从卖家背包抽走，存进 item_info_json + qty，永不丢失——
--     成交交给买家、流拍归还卖家，离线/背包满时进 pending_json 等上线投递。
--   * fee_minor 是创建时即扣的手续费（进国库），无论成交与否都不退。
--   * status: open -> sold(成交) | expired(流拍归还) | cancelled(卖家主动撤，仅限无人出价)

CREATE TABLE auctions (
    auction_id               TEXT PRIMARY KEY,
    seller_account_id       TEXT NOT NULL,
    kind                    TEXT NOT NULL,            -- 'live' | 'offline'
    status                  TEXT NOT NULL DEFAULT 'open',
    item_info_json          TEXT NOT NULL,           -- item_info 字典（含 nbt_b64，潜影盒内容物也保留）
    qty                     INTEGER NOT NULL DEFAULT 1,
    reserve_minor           INTEGER NOT NULL DEFAULT 0,   -- 最低价 / 保留价
    current_bid_minor       INTEGER NOT NULL DEFAULT 0,
    current_bidder_account_id TEXT,
    fee_minor               INTEGER NOT NULL DEFAULT 0,
    escrow_account_id       TEXT,
    start_ms                INTEGER NOT NULL,
    ends_at_ms              INTEGER NOT NULL,
    ended_at_ms             INTEGER,
    version                 INTEGER NOT NULL DEFAULT 0,
    result_json             TEXT,
    pending_json            TEXT,                    -- 未成功投递的物品（玩家离线/背包满）
    updated_at_ms           INTEGER NOT NULL,
    created_at_ms           INTEGER NOT NULL
);

CREATE INDEX ix_auctions_status_ends ON auctions(status, ends_at_ms);
CREATE INDEX ix_auctions_seller ON auctions(seller_account_id);

CREATE TABLE auction_bids (
    bid_id                  TEXT PRIMARY KEY,
    auction_id              TEXT NOT NULL REFERENCES auctions(auction_id) ON DELETE CASCADE,
    bidder_account_id       TEXT NOT NULL,
    amount_minor            INTEGER NOT NULL,
    is_current              INTEGER NOT NULL DEFAULT 0,   -- 1 = 当前最高出价
    created_at_ms           INTEGER NOT NULL
);

CREATE INDEX ix_auction_bids_auction ON auction_bids(auction_id, created_at_ms);
