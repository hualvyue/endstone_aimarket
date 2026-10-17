"""商店业务：建店、补货、取货、关店。

关于「先动背包还是先写库」的顺序
------------------------------------------------------------------
建店时要扣走店主的货物（或资金），关店时要把剩余物资退回。背包操作不可回滚，
所以顺序必须保证**最坏情况下不会丢玩家的东西**：

建店：预检 → 事务写库（库存暂记 0） → 扣背包 → 事务回填实际数量
      即便回填前崩溃，最坏是留下一个 0 库存的空店（可清理），而不是玩家的物品消失。

关店：事务内先把店标记 CLOSED（让它立刻不可交互） → 退物资 → 事务内删除记录
      先关店再退物资，可以杜绝"退了物资但店还在被继续买"的窗口。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core.constants import AccountKind, PricingMode, ShopStatus, ShopType, TradeDirection
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import new_uuid7
from ..core.money import format_amount
from ..core.result import Result
from ..economy.repository import TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from ..inventory.service import InventoryService
from .pricing import ShopPricing
from .repository import ShopRepository, now_ms


class ShopService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        inventory: InventoryService,
        pricing: ShopPricing,
        *,
        companies=None,
        config=None,
        i18n=None,
        log=None,
    ):
        self.db = db
        self.economy = economy
        self.inventory = inventory
        self.pricing = pricing
        self.companies = companies
        self.config = config
        self.i18n = i18n
        self.log = log
        self.repo = ShopRepository(db)

    # ==================== 配置 ====================

    def max_shops_per_player(self) -> int:
        if self.config is None:
            return 10
        return int(self.config.as_int("shop.max_per_player", 10))

    def tax_bps(self) -> int:
        """默认税率（个人）。保留旧名字，等价于 tax_bps_for(个人)。"""
        if self.config is None:
            return 0
        return int(self.config.as_int("economy.tax_bps", 0))

    def tax_bps_for(self, owner_account_id: Optional[str]) -> int:
        """按店主的主体类型给税率。

        个人走 `economy.tax_bps`；公司走 `company.turnover_tax_bps`（通常更高）——
        这正是公司开店免牌照费的交换条件。
        """
        if owner_account_id and self.companies is not None:
            try:
                return self.companies.tax_bps_for_owner(owner_account_id)
            except Exception:
                pass
        return self.tax_bps()

    def license_fee_minor(self) -> int:
        """个人开实体店的牌照费（不可退）。"""
        if self.config is None:
            return 0
        return int(self.config.as_int("shop.open_price_minor", 0))

    def _charge_license(self, account_id: str) -> Result:
        """收牌照费。

        **公司开店免费**（改为按营业额抽税），个人开店收一笔不可退的牌照费。
        费用进国库，不是凭空消失。
        """
        fee = self.license_fee_minor()
        if fee <= 0:
            return Result.success(0)
        account = self.economy.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if account.kind == AccountKind.COMPANY.value:
            return Result.success(0)  # 公司免牌照费

        if account.available_minor < fee:
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"开店需要支付牌照费 {format_amount(fee)}，当前可用 "
                f"{format_amount(account.available_minor)}",
            )
        result = self.economy.transfer(
            account_id,
            TREASURY_ACCOUNT_ID,
            fee,
            reason="shop_license",
            idem=f"license:{account_id}:{new_uuid7()}",
            actor_account_id=account_id,
        )
        if not result.ok:
            return Result.failure(result.code, result.message)
        return Result.success(fee)

    # ==================== 建店 ====================

    def create_sell_shop(
        self,
        *,
        player,
        account_id: str,
        dimension: str,
        x: int,
        y: int,
        z: int,
        item_info: Dict[str, Any],
        quantity: int,
        pricing_mode: str = PricingMode.MANUAL.value,
        unit_minor: Optional[int] = None,
        discount_bps: int = 0,
        is_infinite: bool = False,
    ) -> Result[Dict[str, Any]]:
        """开一个「出售店」：店主把货物摆出来，玩家花钱买走。"""
        guard = self._guard_position(dimension, x, y, z)
        if not guard.ok:
            return guard
        if not is_infinite:
            limit = self._guard_limit(account_id)
            if not limit.ok:
                return limit
            fee = self._charge_license(account_id)
            if not fee.ok:
                return fee
        if int(quantity) <= 0 and not is_infinite:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "库存数量必须大于 0")

        if pricing_mode == PricingMode.OFFICIAL.value:
            if not self.pricing.engine.has_rule(item_info.get("type")):
                return Result.failure(ErrorCode.PRICE_NOT_SET, "该物品没有官方定价，无法使用官方定价模式")
        else:
            if not unit_minor or int(unit_minor) <= 0:
                return Result.failure(ErrorCode.INVALID_AMOUNT, "请设置一个大于 0 的单价")

        try:
            with self.db.transaction():
                shop = self.repo.create_shop(
                    owner_account_id=account_id,
                    shop_type=ShopType.SELL.value,
                    dimension=dimension, x=x, y=y, z=z,
                    offer_item=item_info,
                    pricing_mode=pricing_mode,
                    manual_buy_unit_minor=int(unit_minor) if unit_minor else None,
                    discount_bps=discount_bps,
                    stock_qty=0,
                    is_infinite=is_infinite,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        if shop is None:
            return Result.failure(ErrorCode.STORAGE_ERROR, "创建商店失败")

        if is_infinite:
            self.repo.update(shop["shop_id"], {"stock_qty": 0})
            return Result.success(self.repo.get(shop["shop_id"]))

        # 扣走店主的货物作为库存。扣多少算多少，不因为背包问题整单失败。
        taken = self.inventory.take(player, item_info, int(quantity))
        actual = int(taken.value or 0) if taken.ok else 0
        if actual <= 0:
            self.repo.delete(shop["shop_id"])
            return Result.failure(
                taken.code if not taken.ok else ErrorCode.ITEM_NOT_ENOUGH,
                taken.message or "背包中没有足够的物品",
            )
        self.repo.update(shop["shop_id"], {"stock_qty": actual})
        return Result.success(self.repo.get(shop["shop_id"]))

    def create_buy_shop(
        self,
        *,
        player,
        account_id: str,
        dimension: str,
        x: int,
        y: int,
        z: int,
        item_info: Dict[str, Any],
        budget_minor: int,
        pricing_mode: str = PricingMode.MANUAL.value,
        unit_minor: Optional[int] = None,
        discount_bps: int = 0,
        is_infinite: bool = False,
    ) -> Result[Dict[str, Any]]:
        """开一个「收购店」：店主注入预算，玩家把东西卖给他。

        预算存进独立托管账户，收购时从托管账户出钱 —— 预算与资金是同一份数据，
        不会出现「账上有钱但商店说没预算」的错位。
        """
        guard = self._guard_position(dimension, x, y, z)
        if not guard.ok:
            return guard
        if not is_infinite:
            limit = self._guard_limit(account_id)
            if not limit.ok:
                return limit
            fee = self._charge_license(account_id)
            if not fee.ok:
                return fee

        if pricing_mode == PricingMode.OFFICIAL.value:
            if not self.pricing.engine.has_rule(item_info.get("type")):
                return Result.failure(ErrorCode.PRICE_NOT_SET, "该物品没有官方定价，无法使用官方定价模式")
            unit_minor = None
        else:
            if not unit_minor or int(unit_minor) <= 0:
                return Result.failure(ErrorCode.INVALID_AMOUNT, "请设置一个大于 0 的收购单价")
            if int(budget_minor) <= 0:
                return Result.failure(ErrorCode.INVALID_AMOUNT, "请注入大于 0 的收购预算")

        account = self.economy.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if account.available_minor < int(budget_minor):
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，需要 {format_amount(int(budget_minor))}，"
                f"当前可用 {format_amount(account.available_minor)}",
            )

        try:
            with self.db.transaction():
                escrow_id = f"escrow:{account_id[:12]}:{now_ms()}"
                self.economy.repo.create_account(
                    escrow_id, "escrow", name=f"{account.current_name or '商店'}的收购托管"
                )
                shop = self.repo.create_shop(
                    owner_account_id=account_id,
                    shop_type=ShopType.BUY.value,
                    dimension=dimension, x=x, y=y, z=z,
                    offer_item=item_info,
                    pricing_mode=pricing_mode,
                    manual_sell_unit_minor=int(unit_minor) if unit_minor else None,
                    discount_bps=discount_bps,
                    stock_qty=0,
                    escrow_account_id=escrow_id,
                    is_infinite=is_infinite,
                )
                if shop is None:
                    raise AimarketError(ErrorCode.STORAGE_ERROR, "创建商店失败")

                if not is_infinite:
                    moved = self.economy.post_in_transaction(
                        kind="shop_restock",
                        entries=[
                            _payer(account_id, -int(budget_minor)),
                            _payee(escrow_id, int(budget_minor)),
                        ],
                        idem=f"restock:{shop['shop_id']}",
                        tx_id=f"restock:{shop['shop_id']}",
                        actor=account_id,
                        correlation_id=shop["shop_id"],
                        gross=int(budget_minor),
                        net=int(budget_minor),
                    )
                    if not moved.ok:
                        raise AimarketError(moved.code, moved.message)
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        return Result.success(self.repo.get(shop["shop_id"]))

    def create_barter_shop(
        self,
        *,
        player,
        account_id: str,
        dimension: str,
        x: int, y: int, z: int,
        give_item: Dict[str, Any],
        cost_item: Dict[str, Any],
        give_per_lot: int,
        cost_per_lot: int,
        quantity: int,
    ) -> Result[Dict[str, Any]]:
        """开一个以物易物商店：交出 cost_item × cost_per_lot，换走 give_item × give_per_lot。"""
        guard = self._guard_position(dimension, x, y, z)
        if not guard.ok:
            return guard
        limit = self._guard_limit(account_id)
        if not limit.ok:
            return limit
        fee = self._charge_license(account_id)
        if not fee.ok:
            return fee

        if not give_item or not cost_item:
            return Result.failure(ErrorCode.SHOP_BAD_CONFIG, "以物易物需要同时指定两种物品")
        if int(give_per_lot) <= 0 or int(cost_per_lot) <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "兑换比例必须大于 0")
        if int(quantity) <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "库存数量必须大于 0")

        try:
            with self.db.transaction():
                shop = self.repo.create_shop(
                    owner_account_id=account_id,
                    shop_type=ShopType.BARTER.value,
                    dimension=dimension, x=x, y=y, z=z,
                    offer_item=give_item,
                    cost_item=cost_item,
                    offer_per_lot=int(give_per_lot),
                    cost_per_lot=int(cost_per_lot),
                    stock_qty=0,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        if shop is None:
            return Result.failure(ErrorCode.STORAGE_ERROR, "创建商店失败")

        taken = self.inventory.take(player, give_item, int(quantity))
        actual = int(taken.value or 0) if taken.ok else 0
        if actual <= 0:
            self.repo.delete(shop["shop_id"])
            return Result.failure(ErrorCode.ITEM_NOT_ENOUGH, "背包中没有足够的货物")
        self.repo.update(shop["shop_id"], {"stock_qty": actual})
        return Result.success(self.repo.get(shop["shop_id"]))

    # ==================== 补货 / 注入 ====================

    def restock_sell_shop(self, *, player, shop: Dict[str, Any], quantity: int) -> Result[int]:
        """给出售店补货：从店主背包扣货，加到商店库存。"""
        if int(quantity) <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        item_info = _decode(shop.get("offer_item_json"))
        if not item_info:
            return Result.failure(ErrorCode.SHOP_BAD_CONFIG, "商店商品数据损坏")

        taken = self.inventory.take(player, item_info, int(quantity))
        if not taken.ok:
            return Result.failure(taken.code, taken.message)
        actual = int(taken.value or 0)
        if actual <= 0:
            return Result.failure(ErrorCode.ITEM_NOT_ENOUGH)
        self.db.execute(
            "UPDATE shops SET stock_qty = stock_qty + ?, updated_at_ms = ? WHERE shop_id = ?",
            (actual, now_ms(), shop["shop_id"]),
        )
        return Result.success(actual)

    def inject_budget(self, *, account_id: str, shop: Dict[str, Any], amount_minor: int) -> Result[int]:
        """给收购店追加预算。"""
        if int(amount_minor) <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        escrow = shop.get("escrow_account_id")
        if not escrow:
            return Result.failure(ErrorCode.SHOP_BAD_CONFIG, "该商店没有托管账户")
        account = self.economy.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if account.available_minor < int(amount_minor):
            return Result.failure(
                ErrorCode.INSUFFICIENT_FUNDS,
                f"可用余额不足，当前可用 {format_amount(account.available_minor)}",
            )
        result = self.economy.transfer(
            account_id, escrow, int(amount_minor),
            reason=f"shop_restock:{shop['shop_id']}",
            idem=f"inject:{shop['shop_id']}:{now_ms()}",
        )
        if not result.ok:
            return Result.failure(result.code, result.message)
        return Result.success(int(amount_minor))

    # ==================== 取回收货 ====================

    def collect_items(self, *, player, shop: Dict[str, Any], index: Optional[int] = None) -> Result[Dict[str, Any]]:
        """把收购店收到的物品取回背包。index 为 None 表示全部取回。"""
        collected = self.repo.collected_of(shop)
        if not collected:
            return Result.success({"taken": 0, "left": 0})

        targets = collected if index is None else [collected[int(index)]] if 0 <= int(index) < len(collected) else []
        if not targets:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "没有可取的物品")

        moved = 0
        remaining: List[Dict[str, Any]] = [] if index is None else list(collected)
        for record in targets:
            info = _record_to_item(record)
            count = int(record.get("count", 0) or 0)
            if not info or count <= 0:
                continue
            given = self.inventory.give(player, info, count)
            actual = int(given.value or 0) if given.ok else 0
            if actual <= 0:
                if index is not None:
                    return Result.failure(ErrorCode.INVENTORY_FULL, "背包已满，无法取回")
                remaining.append(record)
                continue
            moved += actual
            if actual < count:
                record = dict(record)
                record["count"] = count - actual
                remaining.append(record)

        if index is not None:
            if moved <= 0:
                return Result.failure(ErrorCode.INVENTORY_FULL, "背包已满，无法取回")
            remaining = [r for i, r in enumerate(collected) if i != int(index)]

        self.repo.set_collected(shop["shop_id"], remaining)
        return Result.success({"taken": moved, "left": len(remaining)})

    # ==================== 关店 ====================

    def close_shop(self, *, player, shop: Dict[str, Any]) -> Result[Dict[str, Any]]:
        """关店并退还：未售出的库存、收购到的物品、托管里剩余的预算，全部还给店主。"""
        shop_id = shop["shop_id"]
        # 先标记为已关闭，确保没有新的交易能进来
        self.repo.set_status(shop_id, ShopStatus.CLOSED.value)

        returned_items = 0
        returned_money = 0
        notes: List[str] = []

        item_info = _decode(shop.get("offer_item_json"))
        stock = int(shop.get("stock_qty", 0) or 0)
        if stock > 0 and item_info and shop.get("shop_type") == ShopType.SELL.value:
            given = self.inventory.give(player, item_info, stock)
            actual = int(given.value or 0) if given.ok else 0
            returned_items += actual
            if actual < stock:
                notes.append(f"背包空间不足，剩余 {stock - actual} 个货物未能退还，已保留在商店记录中")
                self.repo.set_status(shop_id, ShopStatus.ACTIVE.value)
                self.repo.update(shop_id, {"stock_qty": stock - actual})
                return Result.failure(ErrorCode.INVENTORY_FULL, "；".join(notes))

        for record in self.repo.collected_of(shop):
            info = _record_to_item(record)
            count = int(record.get("count", 0) or 0)
            if not info or count <= 0:
                continue
            given = self.inventory.give(player, info, count)
            actual = int(given.value or 0) if given.ok else 0
            returned_items += actual
            if actual < count:
                notes.append(f"背包空间不足，{count - actual} 个收货未能退还")
                self.repo.set_status(shop_id, ShopStatus.ACTIVE.value)
                return Result.failure(ErrorCode.INVENTORY_FULL, "；".join(notes))

        escrow = shop.get("escrow_account_id")
        if escrow:
            balance = self.economy.get_balance(escrow).available_minor
            if balance > 0:
                moved = self.economy.transfer(
                    escrow, shop["owner_account_id"], balance,
                    reason=f"shop_close:{shop_id}",
                    idem=f"close:{shop_id}",
                )
                if moved.ok:
                    returned_money = balance
                else:
                    notes.append(f"托管资金 {format_amount(balance)} 退还失败：{moved.message}")
                    self.repo.set_status(shop_id, ShopStatus.ACTIVE.value)
                    return Result.failure(moved.code, "；".join(notes))

        self.repo.delete(shop_id)
        return Result.success(
            {
                "returned_items": returned_items,
                "returned_money": returned_money,
                "notes": notes,
            }
        )

    # ==================== 查询封装 ====================

    def get_at(self, x: int, y: int, z: int, dimension: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_at(x, y, z, dimension)

    def get(self, shop_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get(shop_id)

    def my_shops(self, account_id: str) -> List[Dict[str, Any]]:
        return self.repo.get_by_owner(account_id)

    def nearby(self, x: int, z: int, dimension: str, radius_chunks: int = 1) -> List[Dict[str, Any]]:
        return self.repo.get_nearby(x, z, dimension, radius_chunks)

    def all_shops(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.repo.get_all(limit=limit)

    def price_of(self, shop: Dict[str, Any], direction: str) -> Optional[int]:
        return self.pricing.unit_minor(shop, direction)

    # ==================== 内部校验 ====================

    def _guard_position(self, dimension: str, x: int, y: int, z: int) -> Result[None]:
        existing = self.repo.get_at(x, y, z, dimension)
        if existing is not None:
            if existing["status"] == ShopStatus.ACTIVE.value:
                return Result.failure(ErrorCode.SHOP_OCCUPIED, "这个按钮上已经有商店了")
            self.repo.delete(existing["shop_id"])
        return Result.success(None)

    def _guard_limit(self, account_id: str) -> Result[None]:
        limit = self.max_shops_per_player()
        if limit <= 0:
            return Result.success(None)
        count = self.repo.count_by_owner(account_id)
        if count >= limit:
            return Result.failure(ErrorCode.SHOP_LIMIT, f"每人最多只能开 {limit} 个商店")
        return Result.success(None)


# ---------- 小工具 ----------

def _decode(payload) -> Dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_to_item(record: Dict[str, Any]) -> Dict[str, Any]:
    info = {
        "type": record.get("type"),
        "name": record.get("name"),
        "count": int(record.get("count", 0) or 0),
        "data": int(record.get("data", 0) or 0),
        "enchants": record.get("enchants") or {},
        "lore": record.get("lore") or [],
    }
    if record.get("nbt_b64"):
        info["nbt_b64"] = record["nbt_b64"]
    return info


def _payer(account_id: str, delta: int):
    from ..economy.models import Entry, ROLE_PAYER

    return Entry(account_id, ROLE_PAYER, delta)


def _payee(account_id: str, delta: int):
    from ..economy.models import Entry, ROLE_PAYEE

    return Entry(account_id, ROLE_PAYEE, delta)
