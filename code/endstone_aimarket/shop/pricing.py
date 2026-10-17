"""商店价格：唯一入口。

单价的来源必须只有一处 —— 若创建时写快照、面板显示实时算、结算时又算一次，
三处可以各不相同，玩家看到的价格和实际扣的钱就可能不是同一个数。

这里全部收敛到 `quote(shop, direction)`：
  * manual 模式   —— 用店主设定的单价，包成一个 PriceQuote 返回，保持接口一致；
  * official 模式 —— 向定价引擎实时取价，带上 price_version 供过期校验。

UI 展示、下单确认、结算扣款三处都调它，不允许任何地方自己算。
"""
from __future__ import annotations

from typing import Optional

from ..core.constants import PricingMode, TradeDirection
from ..core.errors import ErrorCode
from ..core.money import format_amount
from ..core.result import Result
from ..market.models import PriceQuote


class ShopPricing:
    def __init__(self, engine, config=None, log=None):
        self.engine = engine
        self.config = config
        self.log = log

    # ==================== 报价 ====================

    def quote(self, shop: dict, direction: str) -> Result[PriceQuote]:
        """给某个商店的某个方向报价。direction: 'buy' 玩家买入 / 'sell' 玩家卖出。"""
        if shop.get("pricing_mode") == PricingMode.OFFICIAL.value:
            return self._official_quote(shop, direction)
        return self._manual_quote(shop, direction)

    def unit_minor(self, shop: dict, direction: str) -> Optional[int]:
        result = self.quote(shop, direction)
        if not result.ok or result.value.suspended:
            return None
        return int(result.value.unit_minor)

    def _official_quote(self, shop: dict, direction: str) -> Result[PriceQuote]:
        item_key = self._item_key(shop)
        if not item_key:
            return Result.failure(ErrorCode.SHOP_BAD_CONFIG, "商店商品数据损坏")
        discount = int(shop.get("discount_bps", 0) or 0)
        result = self.engine.get_quote(item_key, direction, discount_bps=discount)
        if not result.ok:
            return result
        if result.value.suspended:
            # 保留报价内容，但标记为不可交易，让上层能给出具体原因
            return result
        return result

    def _manual_quote(self, shop: dict, direction: str) -> Result[PriceQuote]:
        key = "manual_buy_unit_minor" if direction == TradeDirection.BUY.value else "manual_sell_unit_minor"
        unit = shop.get(key)
        if unit is None or int(unit) <= 0:
            return Result.failure(ErrorCode.PRICE_NOT_SET, "该商店没有设置这个方向的价格")
        return Result.success(
            PriceQuote(
                item_key=self._item_key(shop) or "",
                direction=direction,
                unit_minor=int(unit),
                base_minor=int(unit),
                price_version=0,
                breakdown={"base": int(unit), "mode": "manual"},
            )
        )

    # ==================== 判断 ====================

    def can_buy(self, shop: dict) -> bool:
        """玩家能不能从这个商店买东西。"""
        if shop.get("shop_type") == "barter":
            return True
        result = self.quote(shop, TradeDirection.BUY.value)
        return result.ok and not result.value.suspended

    def can_sell(self, shop: dict) -> bool:
        """玩家能不能卖东西给这个商店。"""
        if shop.get("shop_type") == "barter":
            return False
        result = self.quote(shop, TradeDirection.SELL.value)
        return result.ok and not result.value.suspended

    def buy_suspended_reason(self, shop: dict) -> str:
        result = self.quote(shop, TradeDirection.BUY.value)
        if result.ok:
            return result.value.suspend_reason if result.value.suspended else ""
        return result.message

    def sell_suspended_reason(self, shop: dict) -> str:
        result = self.quote(shop, TradeDirection.SELL.value)
        if result.ok:
            return result.value.suspend_reason if result.value.suspended else ""
        return result.message

    # ==================== 展示 ====================

    def price_line(self, shop: dict, direction: str, i18n=None) -> str:
        """给按钮/详情页用的一行价格描述。"""
        result = self.quote(shop, direction)
        if not result.ok:
            return _t(i18n, "shop.price_unset", "未定价")
        quote = result.value
        if quote.suspended:
            return _t(i18n, "shop.price_suspended", "暂停交易")
        # 金额格式化统一走 money 模块，避免各处自己用除法算小数
        return format_amount(quote.unit_minor, with_symbol=False)

    def price_detail(self, shop: dict, direction: str) -> str:
        result = self.quote(shop, direction)
        if not result.ok:
            return result.message or "未定价"
        return result.value.describe()

    # ==================== 内部 ====================

    @staticmethod
    def _item_key(shop: dict) -> Optional[str]:
        import json

        raw = shop.get("offer_item_json")
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data.get("type") if isinstance(data, dict) else None
        except Exception:
            return None

    def price_version_of(self, shop: dict, direction: str) -> int:
        result = self.quote(shop, direction)
        if not result.ok:
            return 0
        return int(result.value.price_version or 0)


def _t(i18n, key: str, default: str) -> str:
    if i18n is None:
        return default
    return i18n.t(key)
