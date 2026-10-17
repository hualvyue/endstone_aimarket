"""商店的持久化。

注意一个刻意的设计：**收购店的「预算」不存字段，就是 escrow 账户的余额**。
预算单独存一份的话，「账户里的钱」和「商店的预算」就是两份数据，
任何一次更新漏掉都会对不上。让预算干脆等于账户余额，天然共享事务保护。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import PricingMode, ShopStatus, ShopType
from ..core.database import Database
from ..core.ids import new_uuid7

CHUNK_SIZE = 16


def now_ms() -> int:
    return int(time.time() * 1000)


def chunk_of(value: int) -> int:
    return value // CHUNK_SIZE


class ShopRepository:
    def __init__(self, db: Database):
        self.db = db

    # ==================== 创建 / 删除 ====================

    def create_shop(
        self,
        *,
        owner_account_id: str,
        shop_type: str,
        dimension: str,
        x: int,
        y: int,
        z: int,
        offer_item: Dict[str, Any],
        pricing_mode: str = PricingMode.MANUAL.value,
        cost_item: Optional[Dict[str, Any]] = None,
        offer_per_lot: int = 1,
        cost_per_lot: int = 1,
        manual_buy_unit_minor: Optional[int] = None,
        manual_sell_unit_minor: Optional[int] = None,
        discount_bps: int = 0,
        stock_qty: int = 0,
        escrow_account_id: Optional[str] = None,
        is_infinite: bool = False,
    ) -> Optional[Dict[str, Any]]:
        shop_id = new_uuid7()
        ts = now_ms()
        self.db.insert(
            "shops",
            {
                "shop_id": shop_id,
                "owner_account_id": owner_account_id,
                "escrow_account_id": escrow_account_id,
                "shop_type": shop_type,
                "pricing_mode": pricing_mode,
                "dimension": dimension,
                "x": int(x),
                "y": int(y),
                "z": int(z),
                "chunk_x": chunk_of(int(x)),
                "chunk_z": chunk_of(int(z)),
                "offer_item_json": json.dumps(offer_item, ensure_ascii=False),
                "cost_item_json": json.dumps(cost_item, ensure_ascii=False) if cost_item else None,
                "offer_per_lot": max(1, int(offer_per_lot)),
                "cost_per_lot": max(1, int(cost_per_lot)),
                "manual_buy_unit_minor": manual_buy_unit_minor,
                "manual_sell_unit_minor": manual_sell_unit_minor,
                "discount_bps": int(discount_bps),
                "stock_qty": int(stock_qty),
                "held_stock_qty": 0,
                "collected_json": None,
                "is_infinite": 1 if is_infinite else 0,
                "status": ShopStatus.ACTIVE.value,
                "version": 0,
                "price_version": 0,
                "created_at_ms": ts,
                "updated_at_ms": ts,
            },
        )
        return self.get(shop_id)

    def delete(self, shop_id: str) -> int:
        return self.db.delete("shops", "shop_id = ?", (shop_id,))

    def set_status(self, shop_id: str, status: str) -> int:
        return self.db.update(
            "shops", {"status": status, "updated_at_ms": now_ms()}, "shop_id = ?", (shop_id,)
        )

    def update(self, shop_id: str, data: Dict[str, Any]) -> int:
        payload = dict(data)
        payload["updated_at_ms"] = now_ms()
        return self.db.update("shops", payload, "shop_id = ?", (shop_id,))

    # ==================== 查询 ====================

    def get(self, shop_id: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM shops WHERE shop_id = ?", (shop_id,))

    def get_at(self, x: int, y: int, z: int, dimension: str) -> Optional[Dict[str, Any]]:
        """精确坐标查询，走 (dimension, x, y, z) 唯一索引。"""
        return self.db.query_one(
            "SELECT * FROM shops WHERE dimension = ? AND x = ? AND y = ? AND z = ?",
            (dimension, int(x), int(y), int(z)),
        )

    def get_in_chunk(self, chunk_x: int, chunk_z: int, dimension: str) -> List[Dict[str, Any]]:
        """按区块查询，用于「附近商店」这类批量读取。"""
        return self.db.query_all(
            """SELECT * FROM shops
                WHERE dimension = ? AND chunk_x BETWEEN ? AND ? AND chunk_z BETWEEN ? AND ?
                ORDER BY y DESC""",
            (dimension, chunk_x - 1, chunk_x + 1, chunk_z - 1, chunk_z + 1),
        )

    def get_nearby(self, x: int, z: int, dimension: str, radius_chunks: int = 1) -> List[Dict[str, Any]]:
        cx, cz = chunk_of(int(x)), chunk_of(int(z))
        rows = self.db.query_all(
            """SELECT * FROM shops
                WHERE dimension = ? AND status = ?
                  AND chunk_x BETWEEN ? AND ? AND chunk_z BETWEEN ? AND ?""",
            (dimension, ShopStatus.ACTIVE.value, cx - radius_chunks, cx + radius_chunks,
             cz - radius_chunks, cz + radius_chunks),
        )
        limit = CHUNK_SIZE * max(1, radius_chunks)
        ox, oz = int(x), int(z)
        rows = [r for r in rows if abs(int(r["x"]) - ox) <= limit and abs(int(r["z"]) - oz) <= limit]
        rows.sort(key=lambda r: (int(r["x"]) - ox) ** 2 + (int(r["z"]) - oz) ** 2)
        return rows

    def get_by_owner(self, owner_account_id: str, *, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM shops WHERE owner_account_id = ?"
        params: list = [owner_account_id]
        if active_only:
            sql += " AND status = ?"
            params.append(ShopStatus.ACTIVE.value)
        sql += " ORDER BY updated_at_ms DESC"
        return self.db.query_all(sql, tuple(params))

    def get_all(self, *, active_only: bool = True, limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM shops"
        params: list = []
        if active_only:
            sql += " WHERE status = ?"
            params.append(ShopStatus.ACTIVE.value)
        sql += " ORDER BY updated_at_ms DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query_all(sql, tuple(params))

    def count_by_owner(self, owner_account_id: str, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM shops WHERE owner_account_id = ?"
        params: list = [owner_account_id]
        if active_only:
            sql += " AND status = ?"
            params.append(ShopStatus.ACTIVE.value)
        return int(self.db.scalar(sql, tuple(params), default=0) or 0)

    def count_all(self, *, active_only: bool = True) -> int:
        if active_only:
            return int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM shops WHERE status = ?",
                    (ShopStatus.ACTIVE.value,),
                    default=0,
                ) or 0
            )
        return int(self.db.scalar("SELECT COUNT(*) FROM shops", default=0) or 0)

    # ==================== 收购店收货 ====================

    def collected_of(self, shop: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = shop.get("collected_json")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def set_collected(self, shop_id: str, items: List[Dict[str, Any]]) -> None:
        self.db.update(
            "shops",
            {
                "collected_json": json.dumps(items, ensure_ascii=False) if items else None,
                "updated_at_ms": now_ms(),
            },
            "shop_id = ?",
            (shop_id,),
        )

    # ==================== 统计 ====================

    def stats(self) -> Dict[str, int]:
        row = self.db.query_one(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS active,
                      SUM(CASE WHEN is_infinite != 0 THEN 1 ELSE 0 END) AS infinite
                 FROM shops""",
            (ShopStatus.ACTIVE.value,),
        ) or {}
        return {
            "total": int(row.get("total") or 0),
            "active": int(row.get("active") or 0),
            "infinite": int(row.get("infinite") or 0),
        }

    def types_breakdown(self) -> Dict[str, int]:
        rows = self.db.query_all("SELECT shop_type, COUNT(*) AS n FROM shops GROUP BY shop_type")
        return {r["shop_type"]: int(r["n"]) for r in rows}
