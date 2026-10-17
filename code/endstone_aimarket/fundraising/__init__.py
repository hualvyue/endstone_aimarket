"""公司募资：投资（换股）/ 融资（换债权）/ 赞助（换广告位）。

独立成一个编排层，是为了让依赖方向保持单向：
`fundraising → {company, loan, stock}`，而不是让那三个互相引用。
"""
from .models import FundraisingRecord, Sponsorship
from .service import (
    KIND_FINANCE,
    KIND_INVEST,
    KIND_SPONSOR,
    FundraisingService,
)

__all__ = [
    "FundraisingService",
    "FundraisingRecord",
    "Sponsorship",
    "KIND_INVEST",
    "KIND_FINANCE",
    "KIND_SPONSOR",
]
