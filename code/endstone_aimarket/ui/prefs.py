"""玩家显示偏好的持久化。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.database import Database

CARRIER_SIDEBAR = "sidebar"
CARRIER_ACTIONBAR = "actionbar"
CARRIER_NONE = "none"

VALID_CARRIERS = (CARRIER_SIDEBAR, CARRIER_ACTIONBAR, CARRIER_NONE)

DEFAULT_REFRESH_TICKS = 40  # 2 秒


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class UiPrefs:
    player_account_id: str
    carrier: str = CARRIER_SIDEBAR
    items: List[str] = field(default_factory=list)
    refresh_ticks: int = DEFAULT_REFRESH_TICKS
    enabled: bool = True
    updated_at_ms: int = 0

    @property
    def is_visible(self) -> bool:
        return self.enabled and self.carrier != CARRIER_NONE and bool(self.items)


class PrefsRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, player_account_id: str) -> Optional[UiPrefs]:
        row = self.db.query_one(
            "SELECT * FROM player_ui_prefs WHERE player_account_id = ?", (player_account_id,)
        )
        if row is None:
            return None
        try:
            items = json.loads(row.get("items_json") or "[]")
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        carrier = row.get("carrier") or CARRIER_SIDEBAR
        if carrier not in VALID_CARRIERS:
            carrier = CARRIER_SIDEBAR
        return UiPrefs(
            player_account_id=row["player_account_id"],
            carrier=carrier,
            items=[str(i) for i in items],
            refresh_ticks=max(10, int(row.get("refresh_ticks") or DEFAULT_REFRESH_TICKS)),
            enabled=bool(row.get("enabled", 1)),
            updated_at_ms=int(row.get("updated_at_ms", 0)),
        )

    def get_or_default(self, player_account_id: str, default_items: Optional[List[str]] = None) -> UiPrefs:
        existing = self.get(player_account_id)
        if existing is not None:
            return existing
        return UiPrefs(
            player_account_id=player_account_id,
            items=list(default_items or []),
        )

    def save(self, prefs: UiPrefs) -> None:
        payload = {
            "carrier": prefs.carrier,
            "items_json": json.dumps(list(prefs.items), ensure_ascii=False),
            "refresh_ticks": max(10, int(prefs.refresh_ticks)),
            "enabled": 1 if prefs.enabled else 0,
            "updated_at_ms": now_ms(),
        }
        existing = self.db.query_one(
            "SELECT player_account_id FROM player_ui_prefs WHERE player_account_id = ?",
            (prefs.player_account_id,),
        )
        if existing:
            self.db.update(
                "player_ui_prefs", payload, "player_account_id = ?", (prefs.player_account_id,)
            )
        else:
            payload["player_account_id"] = prefs.player_account_id
            self.db.insert("player_ui_prefs", payload)
        prefs.updated_at_ms = payload["updated_at_ms"]

    def toggle_item(self, player_account_id: str, key: str, *, default_items=None) -> UiPrefs:
        prefs = self.get_or_default(player_account_id, default_items)
        if key in prefs.items:
            prefs.items = [i for i in prefs.items if i != key]
        else:
            prefs.items.append(key)
        self.save(prefs)
        return prefs

    def set_carrier(self, player_account_id: str, carrier: str, *, default_items=None) -> UiPrefs:
        prefs = self.get_or_default(player_account_id, default_items)
        prefs.carrier = carrier if carrier in VALID_CARRIERS else CARRIER_SIDEBAR
        self.save(prefs)
        return prefs

    def set_enabled(self, player_account_id: str, enabled: bool, *, default_items=None) -> UiPrefs:
        prefs = self.get_or_default(player_account_id, default_items)
        prefs.enabled = bool(enabled)
        self.save(prefs)
        return prefs

    def clear(self, player_account_id: str) -> None:
        self.db.delete("player_ui_prefs", "player_account_id = ?", (player_account_id,))

    def active_accounts(self) -> List[str]:
        rows = self.db.query_all(
            "SELECT player_account_id FROM player_ui_prefs WHERE enabled = 1 AND carrier != ?",
            (CARRIER_NONE,),
        )
        return [r["player_account_id"] for r in rows]

    # ==================== 显示项目录（可被服主覆盖显示名）====================

    def upsert_catalog(
        self, *, item_key: str, display_name: str, description: str = "",
        default_on: bool = False, sort_order: int = 100,
    ) -> None:
        payload = {
            "display_name": display_name,
            "description": description,
            "default_on": 1 if default_on else 0,
            "sort_order": int(sort_order),
        }
        existing = self.db.query_one(
            "SELECT item_key FROM display_item_catalog WHERE item_key = ?", (item_key,)
        )
        if existing:
            self.db.update("display_item_catalog", payload, "item_key = ?", (item_key,))
        else:
            payload["item_key"] = item_key
            self.db.insert("display_item_catalog", payload)

    def catalog(self) -> Dict[str, Dict[str, Any]]:
        rows = self.db.query_all("SELECT * FROM display_item_catalog")
        return {r["item_key"]: r for r in rows}
