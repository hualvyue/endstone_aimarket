"""股价估值：公司真实资产 ± 随机指数。

需求原话是「由公司目前具体有多少真实资产（他的商品、原料等）再加减随机指数」，
对应到这里就是：

    每股真实资产 = 公司净资产(NAV) / 流通股
    股价 = 每股真实资产 × (1 ± 随机指数)

再加一层涨跌停，防止单周期被拉爆。所有运算都是整数 + 基点，不引入浮点误差。
"""
from __future__ import annotations

import random
from typing import Optional, Tuple

from ..core.money import add_bps


class ValuationConfig:
    """估值参数。全部走配置，服主可调。"""

    def __init__(
        self,
        rand_bps: int = 500,          # 随机指数幅度 ±5%
        limit_bps: int = 2000,        # 单周期涨跌停 ±20%
        min_price_minor: int = 1,     # 价格下限
        floor_nav_per_share: int = 1,
    ):
        self.rand_bps = max(0, int(rand_bps))
        self.limit_bps = max(0, int(limit_bps))
        self.min_price_minor = max(1, int(min_price_minor))
        self.floor_nav_per_share = max(1, int(floor_nav_per_share))


def nav_per_share(nav_minor: int, circulating_shares: int, *, fallback_minor: int = 0) -> int:
    """每股真实资产。没有流通股时退回发行价（此时股价只由随机指数决定）。"""
    if circulating_shares <= 0:
        return max(0, int(fallback_minor))
    return max(0, int(nav_minor) // int(circulating_shares))


def compute_price(
    *,
    nav_minor: int,
    circulating_shares: int,
    prev_price_minor: int,
    ipo_price_minor: int,
    cfg: ValuationConfig,
    rng: Optional[random.Random] = None,
) -> Tuple[int, int, int]:
    """算出新股价。返回 (price, nav_per_share, 本次使用的随机指数)。

    先算「每股真实资产」，再叠加噪声，最后套涨跌停。
    """
    base = nav_per_share(nav_minor, circulating_shares, fallback_minor=ipo_price_minor)
    base = max(cfg.floor_nav_per_share, base)

    generator = rng or random
    epsilon = generator.randint(-cfg.rand_bps, cfg.rand_bps) if cfg.rand_bps > 0 else 0

    # 注意是 add_bps（按基点加成）而不是 apply_bps（按基点取比例）——
    # 用错的话股价会变成原值的 epsilon/10000，看起来就像一路跌停。
    raw = add_bps(base, epsilon)
    raw = max(cfg.min_price_minor, int(raw))

    # 涨跌停：以上一周期价格为基准夹住
    if prev_price_minor > 0 and cfg.limit_bps > 0:
        low = prev_price_minor * (10000 - cfg.limit_bps) // 10000
        high = prev_price_minor * (10000 + cfg.limit_bps) // 10000
        raw = max(max(cfg.min_price_minor, low), min(high, raw))

    return int(raw), int(base), int(epsilon)
