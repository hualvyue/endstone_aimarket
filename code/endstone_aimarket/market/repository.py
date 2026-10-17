"""价格状态与市场历史的持久化。"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from ..core.database import Database
from ..core.ids import new_uuid7
from .models import PriceState


def now_ms() -> int:
    return int(time.time() * 1000)


class MarketRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 价格状态 ----------

    def get_state(self, item_key: str) -> Optional[PriceState]:
        row = self.db.query_one("SELECT * FROM price_state WHERE item_key = ?", (item_key,))
        return PriceState.from_row(row) if row else None

    def all_states(self) -> List[PriceState]:
        return [PriceState.from_row(r) for r in self.db.query_all("SELECT * FROM price_state")]

    def upsert_state(self, state: PriceState) -> None:
        existing = self.db.query_one(
            "SELECT item_key FROM price_state WHERE item_key = ?", (state.item_key,)
        )
        payload = {
            "base_buy_minor": int(state.base_buy_minor),
            "base_sell_minor": int(state.base_sell_minor),
            "demand_buy_bps": int(state.demand_buy_bps),
            "demand_sell_bps": int(state.demand_sell_bps),
            "daily_bps": int(state.daily_bps),
            "buy_acc_minor": int(state.buy_acc_minor),
            "sell_acc_minor": int(state.sell_acc_minor),
            "version": int(state.version) + 1,
            "updated_at_ms": now_ms(),
        }
        if existing:
            self.db.update("price_state", payload, "item_key = ?", (state.item_key,))
        else:
            payload["item_key"] = state.item_key
            self.db.insert("price_state", payload)

    def ensure_state(self, item_key: str, base_buy_minor: int, base_sell_minor: int) -> PriceState:
        """保证价格状态存在；基准价变化时只在没有动态偏移的情况下跟随更新。"""
        state = self.get_state(item_key)
        if state is None:
            state = PriceState(
                item_key=item_key,
                base_buy_minor=int(base_buy_minor),
                base_sell_minor=int(base_sell_minor),
                updated_at_ms=now_ms(),
            )
            self.upsert_state(state)
            return self.get_state(item_key)
        return state

    def set_base(self, item_key: str, base_buy_minor: int, base_sell_minor: int) -> None:
        state = self.get_state(item_key)
        if state is None:
            self.ensure_state(item_key, base_buy_minor, base_sell_minor)
            return
        state.base_buy_minor = int(base_buy_minor)
        state.base_sell_minor = int(base_sell_minor)
        self.upsert_state(state)

    def reset_all_adjustments(self) -> int:
        return self.db.update(
            "price_state",
            {
                "demand_buy_bps": 0,
                "demand_sell_bps": 0,
                "daily_bps": 0,
                "buy_acc_minor": 0,
                "sell_acc_minor": 0,
                "updated_at_ms": now_ms(),
            },
            "1 = 1",
        )

    def set_daily(self, item_key: str, bps: int, state: PriceState) -> None:
        state.daily_bps = int(bps)
        self.upsert_state(state)

    # ---------- 市场历史 ----------

    def record_event(
        self,
        *,
        event_type: str,
        item_key: str,
        side: Optional[str] = None,
        quantity: int = 0,
        gross_minor: int = 0,
        unit_minor: Optional[int] = None,
        source_trade_id: Optional[str] = None,
    ) -> str:
        event_id = new_uuid7()
        self.db.insert(
            "market_history",
            {
                "event_id": event_id,
                "event_type": event_type,
                "item_key": item_key,
                "side": side,
                "quantity": int(quantity),
                "gross_minor": int(gross_minor),
                "unit_minor": unit_minor,
                "source_trade_id": source_trade_id,
                "occurred_at_ms": now_ms(),
            },
        )
        return event_id

    def recent_volume(self, item_key: str, *, since_ms: int, side: Optional[str] = None) -> dict:
        sql = (
            "SELECT COALESCE(SUM(quantity), 0) AS qty, COALESCE(SUM(gross_minor), 0) AS gross "
            "FROM market_history WHERE item_key = ? AND occurred_at_ms >= ?"
        )
        params: list = [item_key, int(since_ms)]
        if side:
            sql += " AND side = ?"
            params.append(side)
        row = self.db.query_one(sql, tuple(params))
        return {"quantity": int(row["qty"]) if row else 0, "gross_minor": int(row["gross"]) if row else 0}

    def item_series(self, item_key: str, *, since_ms: int, limit: int = 500) -> List[dict]:
        return self.db.query_all(
            """SELECT event_type, side, quantity, gross_minor, unit_minor, occurred_at_ms
                 FROM market_history WHERE item_key = ? AND occurred_at_ms >= ?
                ORDER BY occurred_at_ms DESC LIMIT ?""",
            (item_key, int(since_ms), int(limit)),
        )

    def top_traded(self, *, since_ms: int, limit: int = 10) -> List[dict]:
        return self.db.query_all(
            """SELECT item_key,
                      SUM(quantity)   AS quantity,
                      SUM(gross_minor) AS gross_minor
                 FROM market_history
                WHERE occurred_at_ms >= ? AND event_type = 'trade'
                GROUP BY item_key
                ORDER BY gross_minor DESC
                LIMIT ?""",
            (int(since_ms), int(limit)),
        )

    def cleanup(self, *, older_than_ms: int) -> int:
        return self.db.delete("market_history", "occurred_at_ms < ?", (int(older_than_ms),))

    # ---------- 风险标记 ----------

    def flag_risk(
        self,
        *,
        flag: str,
        account_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        item_key: Optional[str] = None,
        severity: int = 1,
        details: Optional[dict] = None,
    ) -> str:
        flag_id = new_uuid7()
        self.db.insert(
            "risk_flags",
            {
                "flag_id": flag_id,
                "trade_id": trade_id,
                "account_id": account_id,
                "item_key": item_key,
                "flag": flag,
                "severity": int(severity),
                "details_json": json.dumps(details, ensure_ascii=False) if details else None,
                "created_at_ms": now_ms(),
            },
        )
        return flag_id

    def recent_flags(self, *, since_ms: int, limit: int = 50) -> List[dict]:
        return self.db.query_all(
            "SELECT * FROM risk_flags WHERE created_at_ms >= ? ORDER BY created_at_ms DESC LIMIT ?",
            (int(since_ms), int(limit)),
        )
