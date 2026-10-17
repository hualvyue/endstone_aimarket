"""经济指标采集、查询与 AI 预留接口。

本模块只负责「采数据」和「暴露接口」，不接入任何外部模型。
"""
from .ai_gateway import (
    AIAdvisor,
    AIGateway,
    KIND_BALANCE_ADVICE,
    KIND_DEFICIT,
    KIND_INEQUALITY,
    KIND_INFLATION,
    KIND_MANIPULATION,
    KIND_PRICE_ANOMALY,
    KIND_SHOP_ADVICE,
    MarketSnapshot,
    NoopAdvisor,
    RuleBasedAdvisor,
    Suggestion,
)
from .collector import GRANULARITY_DAY, GRANULARITY_HOUR, MetricsCollector
from .query import MetricsQuery

__all__ = [
    "MetricsCollector",
    "MetricsQuery",
    "AIGateway",
    "AIAdvisor",
    "RuleBasedAdvisor",
    "NoopAdvisor",
    "MarketSnapshot",
    "Suggestion",
    "GRANULARITY_HOUR",
    "GRANULARITY_DAY",
    "KIND_INFLATION",
    "KIND_DEFICIT",
    "KIND_INEQUALITY",
    "KIND_PRICE_ANOMALY",
    "KIND_MANIPULATION",
    "KIND_SHOP_ADVICE",
    "KIND_BALANCE_ADVICE",
]
