"""拍卖系统的持久化层。

所有写操作都假定调用方已处于 Database.transaction() 内；这里只做纯粹的 SQL，
不提交、不处理业务规则。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.database import Database


def now_ms() -> int:
    return int(time.time() * 1000)


class AuctionRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 写 ====================

    def create_auction(self, row: Dict[str, Any]) -> None:
        self.db.insert("auctions", row)

    def create_bid(self, row: Dict[str, Any]) -> None:
        self.db.insert("auction_bids", row)

    def clear_current_flags(self, auction_id: str) -> None:
        self.db.execute(
            "UPDATE auction_bids SET is_current = 0 WHERE auction_id = ?", (auction_id,)
        )

    def set_current_bid(
        self,
        auction_id: str,
        bid_minor: int,
        bidder_account_id: Optional[str],
        ends_at_ms: int,
        version: int,
    ) -> int:
        # 乐观锁：WHERE 带 version，若版本已被结算/其它出价推进则 0 行更新，
        # 调用方据此回滚整个出价事务（避免并发丢更新或"已结算还能出价"）。
        return self.db.update(
            "auctions",
            {
                "current_bid_minor": int(bid_minor),
                "current_bidder_account_id": bidder_account_id,
                "ends_at_ms": int(ends_at_ms),
                "version": int(version) + 1,
                "updated_at_ms": now_ms(),
            },
            "auction_id = ? AND version = ?",
            (auction_id, int(version)),
        )

    def set_status(
        self,
        auction_id: str,
        status: str,
        *,
        result_json: Optional[str] = None,
        pending_json: Optional[str] = None,
    ) -> None:
        data: Dict[str, Any] = {"status": status, "ended_at_ms": now_ms(), "updated_at_ms": now_ms()}
        if result_json is not None:
            data["result_json"] = result_json
        if pending_json is not None:
            data["pending_json"] = pending_json
        self.db.update("auctions", data, "auction_id = ?", (auction_id,))

    def append_pending(self, auction_id: str, pending: List[Dict[str, Any]]) -> None:
        # 空列表即"没有待投递物品"，置 NULL 以便 pending_of 的 IS NOT NULL 过滤能正确排除。
        payload = json.dumps(pending, ensure_ascii=False) if pending else None
        self.db.update(
            "auctions",
            {"pending_json": payload, "updated_at_ms": now_ms()},
            "auction_id = ?",
            (auction_id,),
        )

    def touch_version(self, auction_id: str, version: int) -> None:
        self.db.update(
            "auctions",
            {"version": int(version) + 1, "updated_at_ms": now_ms()},
            "auction_id = ?",
            (auction_id,),
        )

    # ==================== 读 ====================

    def get(self, auction_id: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM auctions WHERE auction_id = ?", (auction_id,))

    def list_open(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM auctions WHERE status = 'open' ORDER BY ends_at_ms ASC LIMIT ?",
            (int(limit),),
        )

    def list_expired_open(self, before_ms: int, limit: int = 50) -> List[Dict[str, Any]]:
        """所有已到点、尚处于 open 的拍卖，结算任务专用。"""
        return self.db.query_all(
            "SELECT * FROM auctions WHERE status = 'open' AND ends_at_ms <= ? "
            "ORDER BY ends_at_ms ASC LIMIT ?",
            (int(before_ms), int(limit)),
        )

    def list_by_seller(self, account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM auctions WHERE seller_account_id = ? ORDER BY created_at_ms DESC LIMIT ?",
            (account_id, int(limit)),
        )

    def list_by_bidder(self, account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query_all(
            """SELECT a.* FROM auctions a
               JOIN auction_bids b ON b.auction_id = a.auction_id
              WHERE b.bidder_account_id = ?
              GROUP BY a.auction_id ORDER BY a.created_at_ms DESC LIMIT ?""",
            (account_id, int(limit)),
        )

    def count_open_by_seller(self, account_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM auctions WHERE seller_account_id = ? AND status = 'open'",
                (account_id,),
                default=0,
            )
            or 0
        )

    def pending_of(self, account_id: str) -> List[Dict[str, Any]]:
        """该玩家有待领取物品的拍卖（流拍归还 / 拍下收货）。"""
        rows = self.db.query_all(
            "SELECT * FROM auctions WHERE pending_json IS NOT NULL AND status != 'open'"
        )
        out = []
        for row in rows:
            pending = _decode_list(row.get("pending_json"))
            if any(p.get("account_id") == account_id for p in pending):
                out.append(row)
        return out


def _decode_list(payload) -> List[Dict[str, Any]]:
    if not payload:
        return []
    try:
        data = json.loads(payload)
        return data if isinstance(data, list) else []
    except Exception:
        return []
