"""交易协调器：TCC（Try - Confirm - Cancel）+ 背包副作用补偿。

为什么是这个模式
------------------------------------------------------------------
一次交易横跨两个世界：
  * **数据库**（余额、库存、流水）—— 可回滚，能被事务保护；
  * **Minecraft 背包**（发放/收取物品）—— 不可回滚，而且可能部分成功（背包会满）。

两者无法放进同一个事务。若「先动一个，再动另一个，失败了随手回滚一下」
且**不检查回滚的返回值**，必然出现三类事故：货发了钱没扣、钱扣了货没发、
物品被静默吞掉。TCC 的意义就在于把补偿做成显式契约。

这里的做法：
  Try     冻结买家资金与商店库存，落一条 APPLYING 的交易和一个 saga 记录   [事务1]
  Mark    saga 置为 APPLYING —— **必须在触碰背包之前单独提交**            [事务2]
  背包    真正发放/收取物品，拿回「实际成交数量」
  Confirm 按实际成交量结算：扣款、入账、计税、扣库存、写流水、更新价格      [事务3]
  Cancel  释放全部冻结，交易置为 CANCELLED                                [事务3']

关键的三个保证：
  1. 部分成交是**一等公民** —— 只按实际入包/扣除的数量收钱，多冻结的部分原样释放，
     绝不制造"虚假退款流水"；
  2. 价格影响只在 Confirm 的同一个事务里写 —— 失败交易永远无法污染物价；
  3. saga 的 phase 在触碰背包前就落盘 —— 崩溃后能区分「还没动背包」和「可能动过」，
     前者自动取消，后者转人工并保持冻结，绝不静默吞钱吞物。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.constants import ShopStatus, TxKind, TxStatus
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import idempotency_key as make_idem, new_uuid7
from ..core.money import apply_bps, format_amount
from ..core.result import Result
from ..economy.models import Entry, ROLE_PAYEE, ROLE_PAYER, ROLE_TAX
from ..economy.repository import SYSTEM_MINT_ACCOUNT_ID, TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from ..inventory import item_codec
from ..inventory.service import InventoryService
from ..market.engine import PricingEngine
from .models import SagaPhase, TradeOutcome, TradeRequest, TradeStatus

# 无限商店的对手方：系统账户。它的余额为负即代表净投放的货币量。
SYSTEM_COUNTERPARTY = SYSTEM_MINT_ACCOUNT_ID


def now_ms() -> int:
    return int(time.time() * 1000)


class _Abort(Exception):
    """内部信号：把失败结果带出流程。"""

    def __init__(self, outcome: TradeOutcome):
        super().__init__(outcome.message or outcome.code.value)
        self.outcome = outcome


class TradeCoordinator:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        inventory: InventoryService,
        pricing: PricingEngine,
        *,
        companies=None,
        config=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.inventory = inventory
        self.pricing = pricing
        self.companies = companies
        self.config = config
        self.log = log
        self._locks: set = set()  # 正在交易的玩家 xuid，用于挡住连点

    # ==================== 对外入口 ====================

    def execute(self, req: TradeRequest) -> TradeOutcome:
        try:
            return self._execute(req)
        except _Abort as abort:
            return abort.outcome
        except AimarketError as e:
            if self.log:
                self.log.error(f"交易失败: {e}")
            return TradeOutcome(ok=False, code=e.code, message=e.message)
        except Exception as e:
            if self.log:
                import traceback
                self.log.error(f"交易异常: {e}\n{traceback.format_exc()}")
            return TradeOutcome(ok=False, code=ErrorCode.INTERNAL, message=str(e))

    def _execute(self, req: TradeRequest) -> TradeOutcome:
        # --- 幂等：同一个 nonce 重复提交，直接把上次的结果还回去 ---
        replay = self._load_replay(req.idempotency_key)
        if replay is not None:
            replay.replay = True
            return replay

        shop = req.shop
        if shop.get("status") != ShopStatus.ACTIVE.value:
            raise _Abort(self._fail(ErrorCode.SHOP_NOT_ACTIVE, "商店当前不可用", req))

        lock_key = f"{req.actor_account_id}:{shop['shop_id']}"
        if lock_key in self._locks:
            raise _Abort(self._fail(ErrorCode.TX_BUSY, "上一笔交易还在处理中，请稍候", req))
        self._locks.add(lock_key)
        try:
            return self._run_trade(req, shop)
        finally:
            self._locks.discard(lock_key)

    # ==================== 主流程 ====================

    def _run_trade(self, req: TradeRequest, shop: Dict[str, Any]) -> TradeOutcome:
        is_buy = req.direction == "buy"          # 玩家买入 = 掏钱拿货
        infinite = int(shop.get("is_infinite", 0)) != 0
        item_info = self._decode_item(shop.get("offer_item_json"))
        if not item_info:
            raise _Abort(self._fail(ErrorCode.SHOP_BAD_CONFIG, "商店商品数据损坏", req))

        tax_bps = self._tax_bps(req)
        unit_minor, price_version, quote_error = self._resolve_unit_price(req, shop)
        if quote_error is not None:
            raise _Abort(quote_error)

        # ---------- 1. 数量与资金的预检（只读） ----------
        quantity = max(0, int(req.quantity))
        if quantity <= 0:
            raise _Abort(self._fail(ErrorCode.INVALID_AMOUNT, "数量必须大于 0", req))

        if is_buy:
            # 库存上限（无限商店不限制）
            if not infinite:
                available_stock = int(shop.get("stock_qty", 0)) - int(shop.get("held_stock_qty", 0))
                if available_stock <= 0:
                    raise _Abort(self._fail(ErrorCode.SHOP_SOLD_OUT, "库存不足", req))
                quantity = min(quantity, available_stock)
            # 背包容量上限
            capacity = self.inventory.capacity_for(req.player, item_info)
            if capacity <= 0:
                raise _Abort(self._fail(ErrorCode.INVENTORY_FULL, "背包已满", req))
            quantity = min(quantity, capacity)
            # 余额上限
            payer_account = req.actor_account_id
            balance = self.economy.get_balance(payer_account)
            unit_with_tax = unit_minor + apply_bps(unit_minor, tax_bps)
            if unit_with_tax <= 0:
                unit_with_tax = 1
            affordable = balance.available_minor // unit_with_tax
            if affordable <= 0:
                raise _Abort(self._fail(ErrorCode.INSUFFICIENT_FUNDS, "余额不足", req))
            quantity = min(quantity, affordable)
        else:
            # 玩家卖出：受持有量与商店预算双重限制
            held = self.inventory.count(req.player, item_info)
            if held <= 0:
                raise _Abort(self._fail(ErrorCode.ITEM_NOT_ENOUGH, "没有可出售的物品", req))
            quantity = min(quantity, held)
            if not infinite:
                budget_account = self._escrow_account(shop)
                if not budget_account:
                    raise _Abort(self._fail(ErrorCode.SHOP_BAD_CONFIG, "收购店未配置资金托管账户", req))
                budget = self.economy.get_balance(budget_account).available_minor
                affordable = budget // max(1, unit_minor)
                if affordable <= 0:
                    raise _Abort(self._fail(ErrorCode.SHOP_NO_BUDGET, "商店收购资金不足", req))
                quantity = min(quantity, affordable)

        if quantity <= 0:
            raise _Abort(self._fail(ErrorCode.INVALID_AMOUNT, "可成交数量为 0", req))
        if quantity < int(req.quantity) and not req.allow_partial:
            raise _Abort(self._fail(ErrorCode.INVALID_AMOUNT, "无法按请求数量成交", req))

        # ---------- 2. Try：冻结 ----------
        trade_id = new_uuid7()
        gross_minor = quantity * unit_minor
        tax_minor = apply_bps(gross_minor, tax_bps)
        if is_buy:
            frozen_total = gross_minor + tax_minor          # 买家需要付的全部
            freeze_account = req.actor_account_id
            frozen_stock = 0 if infinite else quantity
        else:
            frozen_total = gross_minor                      # 商店需要付出的总额
            freeze_account = None if infinite else self._escrow_account(shop)
            frozen_stock = 0

        saga_effects = {
            "flow": "give" if is_buy else "take",
            "item": item_info,
            "quantity": quantity,
            "unit_minor": unit_minor,
            "is_buy": is_buy,
            "infinite": infinite,
        }

        self._try_phase(
            req=req,
            shop=shop,
            trade_id=trade_id,
            quantity=quantity,
            unit_minor=unit_minor,
            price_version=price_version,
            tax_bps=tax_bps,
            gross_minor=gross_minor,
            tax_minor=tax_minor,
            frozen_total=frozen_total,
            frozen_stock=frozen_stock,
            freeze_account=freeze_account,
            effects=saga_effects,
        )

        # ---------- 3. 标记「即将触碰背包」并提交 ----------
        # 这一步必须单独提交：崩溃后只有看到 APPLYING，才知道背包可能已经被改过
        self._mark_applying(trade_id)

        # ---------- 4. 背包副作用 ----------
        filled, bus_error = self._apply_inventory(req, item_info, quantity, is_buy)
        if filled <= 0:
            self._cancel_phase(req, shop, trade_id, frozen_total, frozen_stock, freeze_account)
            raise _Abort(
                self._fail(
                    bus_error or (ErrorCode.INVENTORY_FULL if is_buy else ErrorCode.ITEM_REMOVE_FAILED),
                    "背包操作失败，交易已取消，资金与物品均未变动",
                    req,
                )
            )

        # ---------- 5. Confirm：结算 ----------
        outcome = self._confirm_phase(
            req=req,
            shop=shop,
            trade_id=trade_id,
            item_info=item_info,
            filled=filled,
            requested=quantity,
            unit_minor=unit_minor,
            tax_bps=tax_bps,
            frozen_total=frozen_total,
            frozen_stock=frozen_stock,
            freeze_account=freeze_account,
        )
        return outcome

    # ==================== TCC 三个阶段 ====================

    def _try_phase(
        self,
        *,
        req: TradeRequest,
        shop: Dict[str, Any],
        trade_id: str,
        quantity: int,
        unit_minor: int,
        price_version: Optional[int],
        tax_bps: int,
        gross_minor: int,
        tax_minor: int,
        frozen_total: int,
        frozen_stock: int,
        freeze_account: Optional[str],
        effects: Dict[str, Any],
    ) -> None:
        """Try：冻结资金与库存，落交易与 saga 记录。全部在一个事务里。"""
        with self.db.transaction():
            # 资金冻结：这里直接改 held，不走 EconomyService.freeze 以省一次嵌套事务
            if freeze_account and frozen_total > 0:
                account = self.economy.repo.get_account(freeze_account)
                if account is None:
                    raise _Abort(self._fail(ErrorCode.ACCOUNT_NOT_FOUND, "账户不存在", req))
                if account.available_minor < frozen_total:
                    code = ErrorCode.INSUFFICIENT_FUNDS if req.direction == "buy" else ErrorCode.SHOP_NO_BUDGET
                    raise _Abort(self._fail(code, "可用余额不足以完成本次交易", req))
                self.economy.repo.apply_balance_delta(freeze_account, held_delta=frozen_total)

            # 库存冻结
            if frozen_stock > 0:
                available = int(shop.get("stock_qty", 0)) - int(shop.get("held_stock_qty", 0))
                if available < frozen_stock:
                    raise _Abort(self._fail(ErrorCode.SHOP_SOLD_OUT, "库存不足", req))
                self.db.execute(
                    "UPDATE shops SET held_stock_qty = held_stock_qty + ?, updated_at_ms = ? "
                    "WHERE shop_id = ?",
                    (frozen_stock, now_ms(), shop["shop_id"]),
                )

            self.db.insert(
                "shop_trades",
                {
                    "trade_id": trade_id,
                    "idempotency_key": req.idempotency_key,
                    "shop_id": shop["shop_id"],
                    "actor_account_id": req.actor_account_id,
                    "direction": req.direction,
                    "status": TradeStatus.APPLYING.value,
                    "requested_qty": int(req.quantity),
                    "filled_qty": 0,
                    "unit_minor": int(unit_minor),
                    "gross_minor": int(gross_minor),
                    "tax_bps": int(tax_bps),
                    "tax_minor": int(tax_minor),
                    "net_minor": 0,
                    "frozen_minor": int(frozen_total),
                    "frozen_stock": int(frozen_stock),
                    "price_version": price_version,
                    "ledger_tx_id": None,
                    "risk_flags_json": None,
                    "created_at_ms": now_ms(),
                    "finished_at_ms": None,
                },
            )
            self.db.insert(
                "inventory_sagas",
                {
                    "trade_id": trade_id,
                    "xuid": str(getattr(req.player, "xuid", "") or ""),
                    "player_name": str(getattr(req.player, "name", "") or ""),
                    "phase": SagaPhase.ARMED.value,
                    "effects_json": json.dumps(effects, ensure_ascii=False),
                    "attempts": 0,
                    "last_error": None,
                    "created_at_ms": now_ms(),
                    "updated_at_ms": now_ms(),
                },
            )

    def _mark_applying(self, trade_id: str) -> None:
        """单独提交：宣告「背包可能即将被改动」。"""
        with self.db.transaction():
            self.db.update(
                "inventory_sagas",
                {"phase": SagaPhase.APPLYING.value, "updated_at_ms": now_ms()},
                "trade_id = ?",
                (trade_id,),
            )

    def _apply_inventory(self, req: TradeRequest, item_info: Dict[str, Any], quantity: int, is_buy: bool):
        """执行背包副作用。返回 (实际数量, 错误码或 None)。"""
        try:
            if is_buy:
                result = self.inventory.give(req.player, item_info, quantity)
            else:
                result = self.inventory.take(req.player, item_info, quantity)
        except Exception as e:
            if self.log:
                self.log.error(f"背包操作异常: {e}")
            return 0, ErrorCode.ITEM_GIVE_FAILED if is_buy else ErrorCode.ITEM_REMOVE_FAILED

        if not result.ok:
            return 0, result.code
        return int(result.value or 0), None

    def _confirm_phase(
        self,
        *,
        req: TradeRequest,
        shop: Dict[str, Any],
        trade_id: str,
        item_info: Dict[str, Any],
        filled: int,
        requested: int,
        unit_minor: int,
        tax_bps: int,
        frozen_total: int,
        frozen_stock: int,
        freeze_account: Optional[str],
    ) -> TradeOutcome:
        """Confirm：按**实际成交数量**结算。资金、库存、流水、价格同事务写入。"""
        is_buy = req.direction == "buy"
        infinite = int(shop.get("is_infinite", 0)) != 0

        gross = filled * unit_minor
        tax = apply_bps(gross, tax_bps)

        entries: List[Entry] = []
        if is_buy:
            total = gross + tax
            entries.append(Entry(req.actor_account_id, ROLE_PAYER, -total))
            if infinite:
                entries.append(Entry(SYSTEM_COUNTERPARTY, "counterparty", gross))
            else:
                entries.append(Entry(shop["owner_account_id"], ROLE_PAYEE, gross))
            payer_receipt_net = total
        else:
            net = gross - tax
            entries.append(Entry(req.actor_account_id, ROLE_PAYEE, net))
            if infinite:
                entries.append(Entry(SYSTEM_COUNTERPARTY, "counterparty", -gross))
            else:
                entries.append(Entry(self._escrow_account(shop), "escrow", -gross))
            payer_receipt_net = net
        if tax > 0:
            entries.append(Entry(TREASURY_ACCOUNT_ID, ROLE_TAX, tax))

        kind = TxKind.SHOP_BUY.value if is_buy else TxKind.SHOP_SELL.value
        with self.db.transaction():
            # a) 释放 Try 阶段冻结的全部额度（实际只花掉一部分）
            if freeze_account and frozen_total > 0:
                self.economy.repo.apply_balance_delta(freeze_account, held_delta=-frozen_total)

            # b) 记账（分录自动配平 + 事务内二次校验）
            receipt = self.economy.post_in_transaction(
                kind=kind,
                entries=entries,
                idem=make_idem("shop-trade", trade_id),
                tx_id=f"trade:{trade_id}",
                actor=req.actor_account_id,
                correlation_id=str(shop["shop_id"]),
                metadata={
                    "shop_id": shop["shop_id"],
                    "item": item_info.get("type"),
                    "quantity": filled,
                    "unit_minor": unit_minor,
                    "trade_id": trade_id,
                },
                gross=gross,
                net=payer_receipt_net,
                tax=tax,
            )
            if not receipt.ok:
                # 记账失败极罕见（几乎只可能是余额在冻结后被改），必须回滚并如实告知
                raise AimarketError(receipt.code, receipt.message)

            # c) 库存结算
            if is_buy:
                if not infinite and frozen_stock > 0:
                    self.db.execute(
                        "UPDATE shops SET stock_qty = MAX(0, stock_qty - ?), "
                        "held_stock_qty = MAX(0, held_stock_qty - ?), updated_at_ms = ? "
                        "WHERE shop_id = ?",
                        (filled, frozen_stock, now_ms(), shop["shop_id"]),
                    )
            else:
                # 收购店：把收到的物品记进 collected_json，店主可以随时取回
                self._append_collected(shop, item_info, filled)

            # d) 价格影响：**只在成交事务内**写，失败交易无从污染物价
            rule_based = shop.get("pricing_mode") == "official"
            if rule_based:
                self.pricing.record_trade(
                    item_info.get("type"),
                    req.direction,
                    quantity=filled,
                    gross_minor=gross,
                    unit_minor=unit_minor,
                    trade_id=trade_id,
                )
            else:
                self.pricing.repo.record_event(
                    event_type="trade",
                    item_key=item_info.get("type"),
                    side=req.direction,
                    quantity=filled,
                    gross_minor=gross,
                    unit_minor=unit_minor,
                    source_trade_id=trade_id,
                )

            # e) 公司营业额入账（审计与分红决策用）。
            #    真正的扣税已经在上面的分录里完成了，这里只是记一笔流水 ——
            #    同一个事务，所以不会出现"成交了但营业额没记"。
            if self.companies is not None:
                try:
                    self.companies.record_turnover(
                        shop["owner_account_id"],
                        gross_minor=gross,
                        tax_minor=tax,
                        shop_id=shop["shop_id"],
                        direction=req.direction,
                    )
                except Exception as e:
                    if self.log:
                        self.log.debug(f"记录公司营业额失败（不影响成交）: {e}")

            # f) 收尾
            self.db.update(
                "shop_trades",
                {
                    "status": TradeStatus.FILLED.value,
                    "filled_qty": int(filled),
                    "tax_minor": int(tax),
                    "net_minor": int(payer_receipt_net),
                    "ledger_tx_id": receipt.value.tx_id,
                    "finished_at_ms": now_ms(),
                },
                "trade_id = ?",
                (trade_id,),
            )
            self.db.update(
                "shops",
                {"updated_at_ms": now_ms(), "version": int(shop.get("version", 0)) + 1},
                "shop_id = ?",
                (shop["shop_id"],),
            )
            self.db.delete("inventory_sagas", "trade_id = ?", (trade_id,))

        outcome = TradeOutcome(
            ok=True,
            trade_id=trade_id,
            requested_qty=requested,
            filled_qty=filled,
            unit_minor=unit_minor,
            gross_minor=gross,
            tax_minor=tax,
            item_name=item_info.get("name") or item_info.get("type", "?"),
        )
        if is_buy:
            outcome.paid_minor = gross + tax
        else:
            outcome.received_minor = gross - tax
        return outcome

    def _cancel_phase(
        self,
        req: TradeRequest,
        shop: Dict[str, Any],
        trade_id: str,
        frozen_total: int,
        frozen_stock: int,
        freeze_account: Optional[str],
        *,
        reason: str = "",
        manual: bool = False,
    ) -> None:
        """Cancel：释放冻结。绝不产生任何真实资金或物品变动。"""
        with self.db.transaction():
            if freeze_account and frozen_total > 0:
                total, held = self.economy.repo.read_balance(freeze_account)
                real = min(int(frozen_total), held)
                if real > 0:
                    self.economy.repo.apply_balance_delta(freeze_account, held_delta=-real)
            if frozen_stock > 0:
                self.db.execute(
                    "UPDATE shops SET held_stock_qty = MAX(0, held_stock_qty - ?), updated_at_ms = ? "
                    "WHERE shop_id = ?",
                    (frozen_stock, now_ms(), shop["shop_id"]),
                )
            self.db.update(
                "shop_trades",
                {
                    "status": TradeStatus.MANUAL.value if manual else TradeStatus.CANCELLED.value,
                    "finished_at_ms": now_ms(),
                    "risk_flags_json": json.dumps({"reason": reason}, ensure_ascii=False) if reason else None,
                },
                "trade_id = ?",
                (trade_id,),
            )
            if not manual:
                self.db.delete("inventory_sagas", "trade_id = ?", (trade_id,))

    # ==================== 以物易物 ====================

    def execute_barter(self, req: TradeRequest) -> TradeOutcome:
        """以物易物：玩家交出 cost_item × lots，获得 offer_item × lots。不涉及货币。"""
        try:
            shop = req.shop
            lots = max(0, int(req.quantity))
            if lots <= 0:
                raise _Abort(self._fail(ErrorCode.INVALID_AMOUNT, "兑换份数必须大于 0", req))

            replay = self._load_replay(req.idempotency_key)
            if replay is not None:
                replay.replay = True
                return replay

            offer_info = self._decode_item(shop.get("offer_item_json"))
            cost_info = self._decode_item(shop.get("cost_item_json"))
            if not offer_info or not cost_info:
                raise _Abort(self._fail(ErrorCode.SHOP_BAD_CONFIG, "以物易物配置不完整", req))

            give_per_lot = max(1, int(shop.get("offer_per_lot", 1)))
            cost_per_lot = max(1, int(shop.get("cost_per_lot", 1)))
            infinite = int(shop.get("is_infinite", 0)) != 0

            give_total = lots * give_per_lot
            cost_total = lots * cost_per_lot

            if not infinite:
                available = int(shop.get("stock_qty", 0)) - int(shop.get("held_stock_qty", 0))
                lots = min(lots, available // give_per_lot)
            have = self.inventory.count(req.player, cost_info) // cost_per_lot
            lots = min(lots, have)
            capacity = self.inventory.capacity_for(req.player, offer_info)
            lots = min(lots, capacity // give_per_lot)

            if lots <= 0:
                raise _Abort(self._fail(ErrorCode.ITEM_NOT_ENOUGH, "以物易物条件不满足", req))

            give_total = lots * give_per_lot
            cost_total = lots * cost_per_lot
            trade_id = new_uuid7()
            effects = {
                "flow": "barter",
                "give_item": offer_info,
                "give_quantity": give_total,
                "cost_item": cost_info,
                "cost_quantity": cost_total,
            }

            self._try_phase(
                req=req, shop=shop, trade_id=trade_id, quantity=lots, unit_minor=0,
                price_version=None, tax_bps=0, gross_minor=0, tax_minor=0,
                frozen_total=0, frozen_stock=0 if infinite else give_total,
                freeze_account=None, effects=effects,
            )
            self._mark_applying(trade_id)

            # 先收后发：收不齐就整单取消（比"先发后收"安全，货不会被套走）
            taken = self.inventory.take(req.player, cost_info, cost_total)
            if not taken.ok:
                self._cancel_phase(req, shop, trade_id, 0, 0 if infinite else give_total, None)
                raise _Abort(self._fail(taken.code, "你的物品不足，交易已取消", req))

            given = self.inventory.give(req.player, offer_info, give_total)
            given_qty = int(given.value or 0) if given.ok else 0
            if given_qty < give_total:
                # 发货不足：把已发出去的那部分收回来，再把代价物原样还给玩家
                if given_qty > 0:
                    self.inventory.take(req.player, offer_info, given_qty)
                self.inventory.give(req.player, cost_info, cost_total)
                self._cancel_phase(req, shop, trade_id, 0, 0 if infinite else give_total, None)
                raise _Abort(self._fail(ErrorCode.INVENTORY_FULL, "背包空间不足，交易已取消", req))

            with self.db.transaction():
                if not infinite:
                    self.db.execute(
                        "UPDATE shops SET stock_qty = MAX(0, stock_qty - ?), "
                        "held_stock_qty = MAX(0, held_stock_qty - ?), updated_at_ms = ? "
                        "WHERE shop_id = ?",
                        (give_total, give_total, now_ms(), shop["shop_id"]),
                    )
                self._append_collected(shop, cost_info, cost_total)
                self.pricing.repo.record_event(
                    event_type="barter",
                    item_key=offer_info.get("type"),
                    side="barter",
                    quantity=give_total,
                    gross_minor=0,
                    unit_minor=None,
                    source_trade_id=trade_id,
                )
                self.db.update(
                    "shop_trades",
                    {
                        "status": TradeStatus.FILLED.value,
                        "filled_qty": int(lots),
                        "finished_at_ms": now_ms(),
                    },
                    "trade_id = ?",
                    (trade_id,),
                )
                self.db.delete("inventory_sagas", "trade_id = ?", (trade_id,))

            return TradeOutcome(
                ok=True,
                trade_id=trade_id,
                requested_qty=int(req.quantity),
                filled_qty=lots,
                item_name=offer_info.get("name") or "?",
                flags=["barter"],
            )
        except _Abort as abort:
            return abort.outcome
        except Exception as e:
            if self.log:
                self.log.error(f"以物易物异常: {e}")
            return TradeOutcome(ok=False, code=ErrorCode.INTERNAL, message=str(e))

    # ==================== 辅助 ====================

    def _resolve_unit_price(self, req: TradeRequest, shop: Dict[str, Any]):
        """确定单价。official 模式实时报价，manual 模式用店主定价。

        两种模式都只在这里产出价格，UI、确认、结算共用同一个数字。
        """
        if shop.get("pricing_mode") == "official":
            item_key = self._decode_item(shop.get("offer_item_json")) or {}
            item_type = item_key.get("type")
            if not item_type:
                return 0, None, self._fail(ErrorCode.SHOP_BAD_CONFIG, "商店商品数据损坏", req)
            discount = int(shop.get("discount_bps", 0) or 0)
            quoted = self.pricing.get_quote(item_type, req.direction, discount_bps=discount)
            if not quoted.ok:
                return 0, None, self._fail(quoted.code, "该物品当前没有官方定价", req)
            quote = quoted.value
            if quote.suspended:
                return 0, None, self._fail(ErrorCode.SHOP_BUY_SUSPENDED, quote.suspend_reason, req)
            if req.price_version is not None and int(req.price_version) != int(quote.price_version):
                return 0, None, self._fail(ErrorCode.PRICE_STALE, "价格已变动，请重新确认", req)
            return int(quote.unit_minor), int(quote.price_version), None

        key = "manual_buy_unit_minor" if req.direction == "buy" else "manual_sell_unit_minor"
        unit = shop.get(key)
        if unit is None or int(unit) <= 0:
            return 0, None, self._fail(ErrorCode.PRICE_NOT_SET, "该商店未设置价格", req)
        return int(unit), None, None

    def _tax_bps(self, req: TradeRequest) -> int:
        """确定本次成交的税率。

        优先用调用方显式指定的；没指定就**按店主的主体类型**判定 ——
        公司走更高的营业额税率，个人走普通交易税。

        这一层兜底很重要：如果把税率完全交给调用方，任何一处忘记传参，
        公司就会被按个人税率收税，等于长期少收税且没人会发现。
        """
        if req.tax_bps:
            return int(req.tax_bps)
        if self.companies is not None:
            owner = (req.shop or {}).get("owner_account_id")
            if owner:
                try:
                    return int(self.companies.tax_bps_for_owner(owner))
                except Exception:
                    pass
        if self.config is not None:
            return int(self.config.as_int("economy.tax_bps", 0) or 0)
        return 0

    def _escrow_account(self, shop: Dict[str, Any]) -> Optional[str]:
        return shop.get("escrow_account_id")

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

    def _append_collected(self, shop: Dict[str, Any], item_info: Dict[str, Any], quantity: int) -> None:
        """把收购店收到的物品追加到 collected_json，店主可随时取回。"""
        collected: List[Dict[str, Any]] = []
        if shop.get("collected_json"):
            try:
                loaded = json.loads(shop["collected_json"])
                if isinstance(loaded, list):
                    collected = loaded
            except Exception:
                collected = []
        record = {
            "type": item_info.get("type"),
            "name": item_info.get("name"),
            "count": int(quantity),
            "data": int(item_info.get("data", 0) or 0),
            "enchants": item_info.get("enchants") or {},
            "lore": item_info.get("lore") or [],
            "collect_time_ms": now_ms(),
        }
        if item_info.get("nbt_b64"):
            record["nbt_b64"] = item_info["nbt_b64"]
        collected.append(record)
        self.db.update(
            "shops",
            {"collected_json": json.dumps(collected, ensure_ascii=False), "updated_at_ms": now_ms()},
            "shop_id = ?",
            (shop["shop_id"],),
        )

    def _load_replay(self, idem_key: str) -> Optional[TradeOutcome]:
        row = self.db.query_one(
            "SELECT * FROM shop_trades WHERE idempotency_key = ?", (idem_key,)
        )
        if row is None:
            return None
        status = row["status"]
        if status == TradeStatus.FILLED.value:
            return TradeOutcome(
                ok=True,
                trade_id=row["trade_id"],
                requested_qty=int(row["requested_qty"]),
                filled_qty=int(row["filled_qty"]),
                unit_minor=int(row["unit_minor"]),
                gross_minor=int(row["gross_minor"]),
                tax_minor=int(row["tax_minor"]),
                replay=True,
            )
        if status in (TradeStatus.CANCELLED.value, TradeStatus.MANUAL.value):
            return TradeOutcome(
                ok=False,
                code=ErrorCode.TX_STATE,
                message="这笔交易此前已结束（未成交）",
                trade_id=row["trade_id"],
                replay=True,
            )
        # APPLYING：上一笔还在处理，拒绝重复提交
        return TradeOutcome(
            ok=False,
            code=ErrorCode.TX_BUSY,
            message="这笔交易正在处理中，请稍候",
            trade_id=row["trade_id"],
            replay=True,
        )

    def _fail(self, code: ErrorCode, message: str, req: TradeRequest) -> TradeOutcome:
        return TradeOutcome(ok=False, code=code, message=message, requested_qty=int(req.quantity))

    # ==================== 对外查询 ====================

    def find_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM shop_trades WHERE trade_id = ?", (trade_id,))

    def pending_trades(self) -> List[Dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM shop_trades WHERE status = ? ORDER BY created_at_ms",
            (TradeStatus.APPLYING.value,),
        )

    def describe_money(self, minor: int) -> str:
        return format_amount(minor)
