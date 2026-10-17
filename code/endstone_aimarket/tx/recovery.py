"""崩溃恢复：处理服务端非正常退出后残留的「半截交易」。

判断依据是 saga 的 phase，它被设计成在触碰背包**之前**就已落盘：

    ARMED     —— 冻结已生效，但背包一定没被动过  → 可以安全取消
    APPLYING  —— 背包可能已经改过（也可能改了一半）→ 无法自动判定，转人工

对 APPLYING 的交易，本模块**不会**擅自取消 —— 那样可能让玩家白拿物品或丢失物品。
正确做法是把冻结保留住、标记为 MANUAL，交给管理员用 `/aimarket resolve` 裁决。
宁可让一笔交易卡住等人处理，也不能静默吞掉玩家的东西。
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from ..core.constants import TxStatus
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.result import Result
from ..economy.service import EconomyService
from .models import SagaPhase, TradeStatus

# 超过这么久仍未结算的交易，即使 phase 是 ARMED 也值得管理员看一眼
STALE_MS = 10 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


class TradeRecovery:
    def __init__(self, db: Database, economy: EconomyService, log=None):
        self.db = db
        self.economy = economy
        self.log = log

    # ==================== 启动扫描 ====================

    def scan_and_recover(self) -> Dict[str, int]:
        """服务端启动时调用一次。返回处理统计。"""
        stats = {"armed_cancelled": 0, "applying_manual": 0, "orphan_cancelled": 0}
        rows = self.db.query_all(
            "SELECT * FROM shop_trades WHERE status = ? ORDER BY created_at_ms",
            (TradeStatus.APPLYING.value,),
        )
        for trade in rows:
            saga = self.db.query_one(
                "SELECT * FROM inventory_sagas WHERE trade_id = ?", (trade["trade_id"],)
            )
            phase = saga["phase"] if saga else SagaPhase.APPLYING.value

            if phase == SagaPhase.ARMED.value:
                self._cancel(trade, reason="启动恢复：冻结已释放，背包未被修改")
                stats["armed_cancelled"] += 1
            elif saga is None:
                # 没有 saga 记录：说明交易记录写入了但 saga 没写成功（理论不该发生）
                self._cancel(trade, reason="启动恢复：缺少副作用记录，按未执行处理")
                stats["orphan_cancelled"] += 1
            else:
                self._mark_manual(trade, "启动恢复：背包副作用状态不确定，需人工裁决")
                stats["applying_manual"] += 1

        if any(stats.values()) and self.log:
            self.log.warning(
                f"启动恢复完成：安全取消 {stats['armed_cancelled']} 笔，"
                f"转人工 {stats['applying_manual']} 笔，"
                f"孤儿记录 {stats['orphan_cancelled']} 笔"
            )
        return stats

    # ==================== 人工裁决 ====================

    def list_manual(self) -> List[Dict]:
        return self.db.query_all(
            "SELECT * FROM shop_trades WHERE status = ? ORDER BY created_at_ms",
            (TradeStatus.MANUAL.value,),
        )

    def resolve(self, trade_id: str, *, action: str) -> Result[str]:
        """管理员裁决一笔卡住的交易。

        action = "cancel"  → 释放全部冻结，判定为未成交
        action = "confirm" → 视为已成交，把冻结转为实际扣款（谨慎使用）
        """
        trade = self.db.query_one("SELECT * FROM shop_trades WHERE trade_id = ?", (trade_id,))
        if trade is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND)
        if trade["status"] != TradeStatus.MANUAL.value:
            return Result.failure(ErrorCode.TX_STATE, "该交易不在待裁决状态")

        if action == "cancel":
            self._cancel(trade, reason="管理员裁决：判定未成交")
            return Result.success("已判定为未成交，冻结资金已释放")

        if action == "confirm":
            # 把冻结部分真正扣掉：买家/商店的资金确实花出去了
            frozen = int(trade["frozen_minor"])
            account = trade["actor_account_id"] if trade["direction"] == "buy" else None
            try:
                with self.db.transaction():
                    if account and frozen > 0:
                        self.economy.repo.apply_balance_delta(
                            account, balance_delta=-frozen, held_delta=-frozen
                        )
                    self.db.update(
                        "shop_trades",
                        {
                            "status": TradeStatus.FILLED.value,
                            "filled_qty": int(trade["requested_qty"]),
                            "finished_at_ms": now_ms(),
                        },
                        "trade_id = ?",
                        (trade_id,),
                    )
                    self.db.delete("inventory_sagas", "trade_id = ?", (trade_id,))
            except AimarketError as e:
                return Result.failure(e.code, e.message)
            return Result.success("已判定为成交，冻结资金已划出")

        return Result.failure(ErrorCode.INVALID_AMOUNT, "action 只能是 cancel 或 confirm")

    # ==================== 内部 ====================

    def _cancel(self, trade: Dict, *, reason: str) -> None:
        try:
            with self.db.transaction():
                frozen = int(trade["frozen_minor"])
                frozen_stock = int(trade["frozen_stock"])
                if trade["direction"] == "buy" and frozen > 0:
                    # 买家视角：冻结在买家账户上
                    total, held = self.economy.repo.read_balance(trade["actor_account_id"])
                    real = min(frozen, held)
                    if real > 0:
                        self.economy.repo.apply_balance_delta(
                            trade["actor_account_id"], held_delta=-real
                        )
                elif trade["direction"] != "buy" and frozen > 0:
                    # 商店收购视角：冻结在托管账户上
                    shop = self.db.query_one(
                        "SELECT escrow_account_id FROM shops WHERE shop_id = ?",
                        (trade["shop_id"],),
                    )
                    escrow = shop["escrow_account_id"] if shop else None
                    if escrow:
                        total, held = self.economy.repo.read_balance(escrow)
                        real = min(frozen, held)
                        if real > 0:
                            self.economy.repo.apply_balance_delta(escrow, held_delta=-real)

                if frozen_stock > 0:
                    self.db.execute(
                        "UPDATE shops SET held_stock_qty = MAX(0, held_stock_qty - ?), "
                        "updated_at_ms = ? WHERE shop_id = ?",
                        (frozen_stock, now_ms(), trade["shop_id"]),
                    )

                self.db.update(
                    "shop_trades",
                    {
                        "status": TradeStatus.CANCELLED.value,
                        "finished_at_ms": now_ms(),
                        "risk_flags_json": json.dumps({"reason": reason}, ensure_ascii=False),
                    },
                    "trade_id = ?",
                    (trade["trade_id"],),
                )
                self.db.delete("inventory_sagas", "trade_id = ?", (trade["trade_id"],))
        except Exception as e:
            if self.log:
                self.log.error(f"恢复取消交易 {trade['trade_id']} 失败: {e}")

    def _mark_manual(self, trade: Dict, reason: str) -> None:
        try:
            with self.db.transaction():
                self.db.update(
                    "shop_trades",
                    {
                        "status": TradeStatus.MANUAL.value,
                        "risk_flags_json": json.dumps({"reason": reason}, ensure_ascii=False),
                    },
                    "trade_id = ?",
                    (trade["trade_id"],),
                )
        except Exception as e:
            if self.log:
                self.log.error(f"标记交易 {trade['trade_id']} 为人工失败: {e}")

    # ==================== 自检 ====================

    def audit(self) -> Dict[str, int]:
        """一致性体检：冻结残值、悬挂交易、异常账目。"""
        return {
            "applying_trades": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM shop_trades WHERE status = ?",
                    (TradeStatus.APPLYING.value,),
                    default=0,
                )
                or 0
            ),
            "manual_trades": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM shop_trades WHERE status = ?",
                    (TradeStatus.MANUAL.value,),
                    default=0,
                )
                or 0
            ),
            "held_funds_accounts": int(
                self.db.scalar("SELECT COUNT(*) FROM accounts WHERE held_minor > 0", default=0) or 0
            ),
            "held_stock_shops": int(
                self.db.scalar("SELECT COUNT(*) FROM shops WHERE held_stock_qty > 0", default=0) or 0
            ),
            "unbalanced_ledger": len(self.economy.repo.audit_unbalanced(limit=100)),
            "balance_drift": len(self.economy.repo.audit_balance_drift(limit=100)),
        }

    def release_stale_freezes(self) -> int:
        """释放明显陈旧的、没有任何交易支撑的残留冻结。"""
        released = 0
        rows = self.db.query_all("SELECT account_id, held_minor FROM accounts WHERE held_minor > 0")
        for row in rows:
            pending = self.db.scalar(
                "SELECT COUNT(*) FROM shop_trades WHERE actor_account_id = ? AND status = ?",
                (row["account_id"], TradeStatus.APPLYING.value),
                default=0,
            )
            if int(pending or 0) == 0:
                with self.db.transaction():
                    self.economy.repo.apply_balance_delta(
                        row["account_id"], held_delta=-int(row["held_minor"])
                    )
                released += 1
        return released
