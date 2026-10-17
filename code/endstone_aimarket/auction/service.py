"""拍卖业务核心：创建 / 出价 / 结算 / 流拍 / 取消。

设计原则（与商店、贷款等模块一致）：
1. 资金一律走复式记账 + 每场拍卖独立托管账户，不碰玩家余额的直接加减；
2. 背包物品不可回滚，所以"先抽物品 → 再落库记账"的顺序保证最坏情况下不丢玩家东西；
3. 手续费（fee）创建时即扣、进国库、永不退还；
4. 出价用托管账户冻结，被超过自动退回，结算时再从托管划给卖家；
5. 离线玩家或背包满时，物品进 pending_json，上线即投递，绝不凭空消失。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.constants import TxKind
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7, short_id
from ..core.money import apply_bps, format_amount
from ..core.result import Result
from ..economy.models import ROLE_PAYEE, ROLE_PAYER, Entry
from ..economy.repository import TREASURY_ACCOUNT_ID
from .models import AuctionKind, AuctionStatus
from .repository import AuctionRepository, now_ms


class AuctionService:
    def __init__(
        self,
        db,
        economy,
        inventory,
        *,
        config=None,
        i18n=None,
        log=None,
        resolve_player: Optional[Callable[[str], Any]] = None,
    ):
        self.db = db
        self.economy = economy
        self.inventory = inventory
        self.config = config
        self.i18n = i18n
        self.log = log
        self.repo = AuctionRepository(db)
        # 把"账户 -> 在线玩家对象"的解析交给插件，便于离线投递
        self.resolve_player = resolve_player

    # ==================== 配置 ====================

    def enabled(self) -> bool:
        if self.config is None:
            return True
        return self.config.as_bool("auction.enabled", True)

    def _fee_bps(self) -> int:
        return int(self.config.as_int("auction.fee_bps", 200) if self.config else 200)

    def _min_fee_minor(self) -> int:
        return int(self.config.as_int("auction.min_fee_minor", 1000) if self.config else 1000)

    def _max_qty(self) -> int:
        return int(self.config.as_int("auction.max_qty_per_listing", 1000) if self.config else 1000)

    def _offline_seconds(self) -> int:
        return int(self.config.as_int("auction.offline_seconds", 86400) if self.config else 86400)

    def _live_seconds(self) -> int:
        return int(self.config.as_int("auction.live_seconds", 60) if self.config else 60)

    def _live_extend_ms(self) -> int:
        return int(self.config.as_int("auction.live_extend_ms", 10000) if self.config else 10000)

    def _live_max_seconds(self) -> int:
        return int(self.config.as_int("auction.live_max_seconds", 300) if self.config else 300)

    def fee_for(self, reserve_minor: int) -> int:
        """手续费 = max(最低手续费, 保留价 × 费率)。保留价为 0 时按最低手续费收。"""
        return max(self._min_fee_minor(), apply_bps(max(0, int(reserve_minor)), self._fee_bps()))

    # ==================== 创建拍卖 ====================

    def create_auction(
        self,
        *,
        player,
        account_id: str,
        item_info: Dict[str, Any],
        quantity: int,
        kind: str,
        reserve_minor: int,
        duration_seconds: Optional[int] = None,
    ) -> Result[Dict[str, Any]]:
        if not self.enabled():
            return Result.failure(ErrorCode.PERMISSION_DENIED, "拍卖功能未开启")
        if not item_info or not item_info.get("type"):
            return Result.failure(ErrorCode.SHOP_BAD_CONFIG, "物品信息无效")
        qty = int(quantity)
        if qty <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "数量必须大于 0")
        if qty > self._max_qty():
            return Result.failure(ErrorCode.INVALID_AMOUNT, f"单次最多上架 {self._max_qty()} 个")

        kind = AuctionKind.LIVE.value if str(kind).lower() == AuctionKind.LIVE.value else AuctionKind.OFFLINE.value
        reserve_minor = max(0, int(reserve_minor))
        if kind == AuctionKind.OFFLINE.value and reserve_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "离线拍必须设置最低价")

        fee = self.fee_for(reserve_minor)
        balance = self.economy.get_balance(account_id)
        if balance.available_minor < fee:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"上架需要手续费 {format_amount(fee)}，当前可用 {format_amount(balance.available_minor)}",
            )

        # 1) 先把物品从背包抽走（不可回滚）。抽不足则直接失败，物品原封不动。
        item_json = json.dumps(item_info, ensure_ascii=False)
        taken = self.inventory.take(player, item_info, qty)
        actual = int(taken.value or 0) if taken.ok else 0
        if actual <= 0:
            return Result.failure(taken.code if not taken.ok else ErrorCode.ITEM_NOT_ENOUGH,
                                  "背包中没有足够的物品")

        # 2) 落库 + 收手续费，同一事务
        ts = now_ms()
        if duration_seconds and duration_seconds > 0:
            duration_seconds = int(duration_seconds)
        else:
            duration_seconds = self._live_seconds() if kind == AuctionKind.LIVE.value else self._offline_seconds()
        ends_at = ts + duration_seconds * 1000
        auction_id = new_uuid7()
        escrow_id = f"auc:escrow:{short_id(16)}"

        try:
            with self.db.transaction():
                self.economy.repo.create_account(
                    escrow_id, "escrow", name=f"拍卖托管 {short_id(6)}"
                )
                self.repo.create_auction({
                    "auction_id": auction_id,
                    "seller_account_id": account_id,
                    "kind": kind,
                    "status": AuctionStatus.OPEN.value,
                    "item_info_json": item_json,
                    "qty": actual,
                    "reserve_minor": reserve_minor,
                    "current_bid_minor": 0,
                    "current_bidder_account_id": None,
                    "fee_minor": fee,
                    "escrow_account_id": escrow_id,
                    "start_ms": ts,
                    "ends_at_ms": ends_at,
                    "ended_at_ms": None,
                    "version": 0,
                    "result_json": None,
                    "pending_json": None,
                    "updated_at_ms": ts,
                    "created_at_ms": ts,
                })
                if fee > 0:
                    moved = self.economy.transfer(
                        account_id, TREASURY_ACCOUNT_ID, fee,
                        reason="auction_fee", idem=f"auc:fee:{auction_id}",
                        actor_account_id=account_id,
                        kind=TxKind.AUCTION_FEE.value,
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)
        except AimarketError as e:
            # 回滚：把抽走的物品还给玩家
            self.inventory.give(player, item_info, actual)
            return Result.failure(e.code, e.message)
        except Exception as e:  # pragma: no cover
            self.inventory.give(player, item_info, actual)
            return Result.failure(ErrorCode.INTERNAL, str(e))

        return Result.success(self.repo.get(auction_id))

    # ==================== 出价 ====================

    def place_bid(
        self, *, account_id: str, auction_id: str, amount_minor: int
    ) -> Result[Dict[str, Any]]:
        if not self.enabled():
            return Result.failure(ErrorCode.PERMISSION_DENIED, "拍卖功能未开启")
        auction = self.repo.get(auction_id)
        if auction is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND, "拍卖不存在")
        if auction["status"] != AuctionStatus.OPEN.value:
            return Result.failure(ErrorCode.TX_STATE, "该拍卖已结束")
        if now_ms() >= int(auction["ends_at_ms"]):
            # 理论上由结算任务收掉，这里兜底
            return Result.failure(ErrorCode.TX_EXPIRED, "该拍卖已到点")
        if account_id == auction["seller_account_id"]:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "不能给自己的拍卖出价")

        amount = int(amount_minor)
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "出价必须为正")
        reserve = int(auction["reserve_minor"])
        current = int(auction["current_bid_minor"])
        prev_bidder = auction["current_bidder_account_id"]
        prev_amount = current

        # 首次出价需 >= 保留价；之后必须严格高于当前最高（同价先来后到，不抢占）
        if current <= 0:
            if amount < reserve:
                return Result.failure(
                    ErrorCode.INVALID_AMOUNT, f"出价不得低于最低价 {format_amount(reserve)}"
                )
        else:
            if amount <= current:
                return Result.failure(
                    ErrorCode.INVALID_AMOUNT, f"出价必须高于当前最高 {format_amount(current)}"
                )

        balance = self.economy.get_balance(account_id)
        is_self_raise = prev_bidder == account_id
        need = amount if not is_self_raise else (amount - current)
        if balance.available_minor < need:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，需要 {format_amount(need)}，当前 {format_amount(balance.available_minor)}",
            )

        bid_id = new_uuid7()
        escrow = auction["escrow_account_id"]

        try:
            with self.db.transaction():
                if is_self_raise:
                    # 自己加价：只补差额（避免把钱挪进又挪出）
                    if need > 0:
                        moved = self.economy.transfer(
                            account_id, escrow, need,
                            reason="auction_bid", idem=f"auc:take:{bid_id}",
                            actor_account_id=account_id,
                            kind=TxKind.AUCTION_BID.value,
                        )
                        if not moved.ok:
                            raise AimarketError(moved.code, moved.message)
                else:
                    # 先退上一手，再收新手。
                    # 注意：退款幂等键用「本次出价 bid_id」而非「auction:被退者」，
                    # 因为同一个被超过者可能在多轮拉锯中被多次退款（A→B→A→B…），
                    # 用被退者做键会在第二次退同一人时命中旧幂等记录、转账被静默跳过，
                    # 造成「钱没退、escrow 却照收下一手」的资金凭空增发。bid_id 每次唯一，恰好一一对应一次退款。
                    if prev_bidder and prev_amount > 0:
                        refunded = self.economy.transfer(
                            escrow, prev_bidder, prev_amount,
                            reason="auction_bid_refund", idem=f"auc:refund:{bid_id}",
                            actor_account_id=prev_bidder,
                            kind=TxKind.AUCTION_BID_REFUND.value,
                        )
                        if not refunded.ok:
                            raise AimarketError(refunded.code, refunded.message)
                    moved = self.economy.transfer(
                        account_id, escrow, amount,
                        reason="auction_bid", idem=f"auc:take:{bid_id}",
                        actor_account_id=account_id,
                        kind=TxKind.AUCTION_BID.value,
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)

                self.repo.clear_current_flags(auction_id)
                self.repo.create_bid({
                    "bid_id": bid_id,
                    "auction_id": auction_id,
                    "bidder_account_id": account_id,
                    "amount_minor": amount,
                    "is_current": 1,
                    "created_at_ms": now_ms(),
                })

                # 现场拍防狙击：临近结束时出价延长倒计时，但有上限
                ends_at = int(auction["ends_at_ms"])
                if auction["kind"] == AuctionKind.LIVE.value:
                    extend = self._live_extend_ms()
                    cap = int(auction["start_ms"]) + self._live_max_seconds() * 1000
                    if (ends_at - now_ms()) < extend:
                        ends_at = min(now_ms() + extend, cap)
                rows = self.repo.set_current_bid(
                    auction_id, amount, account_id, ends_at, int(auction["version"])
                )
                # 乐观锁：若版本已被结算或其它出价推进（0 行更新），整笔回滚让玩家重试，
                # 杜绝并发丢更新与「已结算/到期仍能出价」的竞态。
                if rows == 0:
                    raise AimarketError(ErrorCode.TX_BUSY, "拍卖状态已变化，请重试出价")
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        except Exception as e:  # pragma: no cover
            return Result.failure(ErrorCode.INTERNAL, str(e))

        return Result.success(self.repo.get(auction_id))

    # ==================== 取消（仅限尚无人出价）====================

    def cancel_auction(self, *, account_id: str, auction_id: str) -> Result[Dict[str, Any]]:
        auction = self.repo.get(auction_id)
        if auction is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND, "拍卖不存在")
        if auction["seller_account_id"] != account_id:
            return Result.failure(ErrorCode.PERMISSION_DENIED, "只能撤销自己的拍卖")
        if auction["status"] != AuctionStatus.OPEN.value:
            return Result.failure(ErrorCode.TX_STATE, "该拍卖已结束，无法撤销")
        if int(auction["current_bid_minor"]) > 0:
            return Result.failure(ErrorCode.TX_BUSY, "已经有人出价，不能撤销（只能等结束）")

        item = self._decode_item(auction["item_info_json"])
        qty = int(auction["qty"])
        try:
            with self.db.transaction():
                self.repo.set_status(auction_id, AuctionStatus.CANCELLED.value)
        except Exception as e:  # pragma: no cover
            return Result.failure(ErrorCode.INTERNAL, str(e))

        # 物品归还卖家（背包操作，离线也可由上线投递处理）
        self._stage_or_deliver(auction_id, account_id, item, qty)
        return Result.success(self.repo.get(auction_id))

    # ==================== 结算（定时任务调用）====================

    def settle_open(self, *, now: Optional[int] = None, limit: int = 50) -> int:
        """扫描所有已到点且仍 open 的拍卖并结算。返回本批结算数量。"""
        cut = int(now if now is not None else now_ms())
        rows = self.repo.list_expired_open(cut, limit=limit)
        done = 0
        for row in rows:
            try:
                self.settle_one(row)
                done += 1
            except Exception as e:
                if self.log:
                    self.log.error(f"拍卖结算失败 {row.get('auction_id')}: {e}")
        return done

    def settle_one(self, auction_row: Dict[str, Any]) -> None:
        auction_id = auction_row["auction_id"]
        current = int(auction_row["current_bid_minor"])
        reserve = int(auction_row["reserve_minor"])
        bidder = auction_row["current_bidder_account_id"]
        seller = auction_row["seller_account_id"]
        escrow = auction_row["escrow_account_id"]
        item = self._decode_item(auction_row["item_info_json"])
        qty = int(auction_row["qty"])

        sold = bidder is not None and current >= reserve
        try:
            with self.db.transaction():
                if sold:
                    moved = self.economy.transfer(
                        escrow, seller, current,
                        reason="auction_sale", idem=f"auc:sale:{auction_id}",
                        actor_account_id=seller,
                        kind=TxKind.AUCTION_SALE.value,
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)
                    result = {
                        "outcome": "sold",
                        "winner_account_id": bidder,
                        "price_minor": current,
                        "fee_minor": int(auction_row["fee_minor"]),
                    }
                    self.repo.set_status(
                        auction_id, AuctionStatus.SOLD.value,
                        result_json=json.dumps(result, ensure_ascii=False),
                    )
                else:
                    # 流拍：物品归还卖家。正常情况下 escrow 余额为 0（离线拍强制首价>=保留价、
                    # 无人出价时本就不冻结资金），但若历史数据异常/规则被绕过导致仍冻结了钱，
                    # 这里原路退回给出价者，避免资金滞留孤儿托管账户。
                    bal = self.economy.get_balance(escrow).available_minor
                    if bidder is not None and bal > 0:
                        refunded = self.economy.transfer(
                            escrow, bidder, bal,
                            reason="auction_bid_refund", idem=f"auc:refund:expire:{auction_id}",
                            actor_account_id=bidder, kind=TxKind.AUCTION_BID_REFUND.value,
                        )
                        if not refunded.ok:
                            raise AimarketError(refunded.code, refunded.message)
                    result = {
                        "outcome": "expired",
                        "reserve_minor": reserve,
                        "reason": "no_valid_bid" if bidder is None else "below_reserve",
                    }
                    self.repo.set_status(
                        auction_id, AuctionStatus.EXPIRED.value,
                        result_json=json.dumps(result, ensure_ascii=False),
                    )
        except AimarketError as e:
            if self.log:
                self.log.error(f"拍卖 {auction_id} 结算记账失败: {e}")
            return
        except Exception as e:  # pragma: no cover
            if self.log:
                self.log.error(f"拍卖 {auction_id} 结算异常: {e}")
            return

        # 物品投递：成交交给买家，流拍归还卖家
        receiver = bidder if sold else seller
        self._stage_or_deliver(auction_id, receiver, item, qty)

    # ==================== 物品投递 ====================

    def _stage_or_deliver(
        self, auction_id: str, account_id: str, item: Dict[str, Any], qty: int
    ) -> None:
        """尽量直接发给在线玩家；离线/背包满则写入 pending_json，待上线投递。"""
        player = self.resolve_player(account_id) if self.resolve_player else None
        if player is None:
            self._add_pending(auction_id, account_id, item, qty)
            return
        given = self.inventory.give(player, item, qty)
        actual = int(given.value or 0) if given.ok else 0
        if actual < qty:
            self._add_pending(auction_id, account_id, item, qty - actual)

    def _add_pending(self, auction_id: str, account_id: str, item: Dict[str, Any], qty: int) -> None:
        row = self.repo.get(auction_id)
        if row is None:
            return
        pending = self._decode_list(row.get("pending_json"))
        pending.append({"account_id": account_id, "item": item, "qty": int(qty)})
        self.repo.append_pending(auction_id, pending)

    def deliver_pending(self, account_id: str, player) -> int:
        """玩家上线时调用：把待领取物品尽量发到背包，返回成功投递的件数。"""
        rows = self.repo.pending_of(account_id)
        delivered = 0
        for row in rows:
            pending = self._decode_list(row.get("pending_json"))
            still = []
            changed = False
            for entry in pending:
                if entry.get("account_id") != account_id:
                    still.append(entry)
                    continue
                given = self.inventory.give(player, entry.get("item", {}), int(entry.get("qty", 0)))
                actual = int(given.value or 0) if given.ok else 0
                delivered += actual
                changed = True
                if actual < int(entry.get("qty", 0)):
                    still.append({**entry, "qty": int(entry.get("qty", 0)) - actual})
            if changed:
                self.repo.append_pending(row["auction_id"], still)
        return delivered

    # ==================== 查询封装 ====================

    def get(self, auction_id: str):
        return self.repo.get(auction_id)

    def list_open(self, limit: int = 100):
        return self.repo.list_open(limit=limit)

    def list_by_seller(self, account_id: str, limit: int = 50):
        return self.repo.list_by_seller(account_id, limit=limit)

    def list_by_bidder(self, account_id: str, limit: int = 50):
        return self.repo.list_by_bidder(account_id, limit=limit)

    def describe(self, auction: Dict[str, Any]) -> str:
        item = self._decode_item(auction.get("item_info_json")) or {}
        name = item.get("name") or item.get("type") or "?"
        qty = int(auction.get("qty", 0))
        reserve = int(auction.get("reserve_minor", 0))
        cur = int(auction.get("current_bid_minor", 0))
        kind = "现场拍" if auction.get("kind") == AuctionKind.LIVE.value else "离线拍"
        left = max(0, int(auction.get("ends_at_ms", 0)) - now_ms())
        mins = left // 60000
        secs = (left % 60000) // 1000
        return (
            f"{name} ×{qty}｜{kind}｜最低价 {format_amount(reserve)}｜"
            f"当前 {format_amount(cur)}｜剩余 {mins}分{secs}秒"
        )

    # ==================== 内部 ====================

    @staticmethod
    def _decode_item(payload) -> Dict[str, Any]:
        if not payload:
            return {}
        if isinstance(payload, dict):
            return payload
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _decode_list(payload) -> List[Dict[str, Any]]:
        if not payload:
            return []
        try:
            data = json.loads(payload)
            return data if isinstance(data, list) else []
        except Exception:
            return []
