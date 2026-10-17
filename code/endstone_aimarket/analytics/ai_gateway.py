"""AI 层预留接口。

本轮不接入任何模型，只把「AI 需要什么输入、应该产出什么」固定下来，
并把数据源准备好（`MetricsCollector` 已经在持续采集）。

接口刻意设计成**纯函数式**：吃一个 `MarketSnapshot`，吐一串 `Suggestion`。
这样将来无论换成规则引擎、本地小模型还是远程大模型，
调用方（管理面板 / 定时任务）一行都不用改。
"""
from __future__ import annotations

import dataclasses
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"


@dataclass
class MarketSnapshot:
    """喂给 AI 的一份经济快照。字段全部来自 metric_samples 与账本。"""

    taken_at_ms: int = 0
    m0_minor: int = 0
    minted_minor: int = 0
    treasury_minor: int = 0
    escrow_minor: int = 0
    holders: int = 0
    accounts: int = 0
    avg_balance_minor: int = 0
    median_balance_minor: int = 0
    gini: float = 0.0
    top10_share_bps: int = 0
    cpi_bps: int = 10000
    inflation_bps: int = 0
    active_shops: int = 0
    trade_qty: int = 0
    trade_gross_minor: int = 0
    hot_items: List[Dict] = field(default_factory=list)
    risk_flags: List[Dict] = field(default_factory=list)
    extra: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)


@dataclass
class Suggestion:
    """AI 给出的一条建议。管理面板直接展示，或由定时任务推送。"""

    kind: str
    severity: str = SEVERITY_INFO
    title: str = ""
    detail: str = ""
    metrics: Dict = field(default_factory=dict)
    created_at_ms: int = 0

    def __post_init__(self):
        if not self.created_at_ms:
            self.created_at_ms = int(time.time() * 1000)


# 建议类型（约定好枚举，方便将来做本地化与图标/颜色映射）
KIND_INFLATION = "inflation"          # 通胀/通缩预警
KIND_DEFICIT = "deficit"              # 国库与回收失衡
KIND_INEQUALITY = "inequality"        # 贫富差距
KIND_PRICE_ANOMALY = "price_anomaly"  # 单品价格异常
KIND_MANIPULATION = "manipulation"    # 疑似操纵/刷金
KIND_SHOP_ADVICE = "shop_advice"      # 商店经营建议
KIND_BALANCE_ADVICE = "balance_advice"  # 参数调优建议


class AIAdvisor(ABC):
    """AI 顾问的抽象接口。"""

    name: str = "base"

    @abstractmethod
    def analyze(self, snapshot: MarketSnapshot) -> List[Suggestion]:
        """输入快照，返回建议列表。实现必须是纯函数，不产生副作用。"""

    def available(self) -> bool:
        return True


class RuleBasedAdvisor(AIAdvisor):
    """基于阈值的保守顾问 —— 不依赖任何模型，但已经能给出可用的预警。

    它同时也是 AI 接口的一个参考实现：将来接模型时，把这份规则输出作为
    兜底（模型不可用时仍然有预警），或者在它之上做增强。
    """

    name = "rule_based"

    def __init__(self, config=None):
        self.config = config

    def analyze(self, snapshot: MarketSnapshot) -> List[Suggestion]:
        out: List[Suggestion] = []

        # 通胀 / 通缩
        if snapshot.inflation_bps >= 1000:
            out.append(Suggestion(
                kind=KIND_INFLATION, severity=SEVERITY_WARN,
                title="物价快速上涨",
                detail=f"物价指数近期上涨 {snapshot.inflation_bps / 100:.2f}%，"
                       f"建议提高回收价或收紧货币投放。",
                metrics={"inflation_bps": snapshot.inflation_bps, "cpi_bps": snapshot.cpi_bps},
            ))
        elif snapshot.inflation_bps <= -1000:
            out.append(Suggestion(
                kind=KIND_INFLATION, severity=SEVERITY_WARN,
                title="物价持续下跌",
                detail=f"物价指数近期下跌 {snapshot.inflation_bps / 100:.2f}%，"
                       f"玩家可能不愿出售物品，建议检查回收价与交易税。",
                metrics={"inflation_bps": snapshot.inflation_bps, "cpi_bps": snapshot.cpi_bps},
            ))

        # 贫富差距
        if snapshot.gini >= 0.7:
            out.append(Suggestion(
                kind=KIND_INEQUALITY, severity=SEVERITY_WARN,
                title="财富高度集中",
                detail=f"基尼系数 {snapshot.gini:.4f}，前 10% 玩家持有 "
                       f"{snapshot.top10_share_bps / 100:.2f}% 的货币。",
                metrics={"gini": snapshot.gini, "top10_share_bps": snapshot.top10_share_bps},
            ))

        # 货币沉淀在商店托管里
        if snapshot.m0_minor > 0:
            locked_bps = snapshot.escrow_minor * 10000 // max(1, snapshot.m0_minor)
            if locked_bps >= 3000:
                out.append(Suggestion(
                    kind=KIND_DEFICIT, severity=SEVERITY_INFO,
                    title="大量货币沉淀在商店托管",
                    detail=f"托管资金占货币总量 {locked_bps / 100:.2f}%，"
                           f"流通性偏低，可考虑提醒店主补货或降低回收价。",
                    metrics={"escrow_minor": snapshot.escrow_minor, "locked_bps": locked_bps},
                ))

        # 市场过热
        if snapshot.m0_minor > 0 and snapshot.trade_gross_minor * 10000 // max(1, snapshot.m0_minor) >= 5000:
            out.append(Suggestion(
                kind=KIND_SHOP_ADVICE, severity=SEVERITY_INFO,
                title="交易活跃度偏高",
                detail="本周期成交额已超过货币总量的一半，注意价格波动可能较大。",
                metrics={"trade_gross_minor": snapshot.trade_gross_minor},
            ))

        # 风险标记
        if snapshot.risk_flags:
            out.append(Suggestion(
                kind=KIND_MANIPULATION, severity=SEVERITY_WARN,
                title="存在异常交易记录",
                detail=f"近期有 {len(snapshot.risk_flags)} 条风险标记，建议人工复核。",
                metrics={"count": len(snapshot.risk_flags)},
            ))

        return out


class NoopAdvisor(AIAdvisor):
    """占位实现：什么都不做。用于未启用分析时的默认装配。"""

    name = "noop"

    def analyze(self, snapshot: MarketSnapshot) -> List[Suggestion]:
        return []

    def available(self) -> bool:
        return False


class AIGateway:
    """AI 网关：装配 advisor、构造快照、触发分析。

    将来接大模型时，只需要实现一个新的 `AIAdvisor` 子类并在这里注册，
    其余代码（面板、定时任务、报表）完全不用改。
    """

    def __init__(self, metrics_query, config=None, log=None):
        self.query = metrics_query
        self.config = config
        self.log = log
        self._advisors: List[AIAdvisor] = []
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._advisors.append(RuleBasedAdvisor(self.config))

    def register(self, advisor: AIAdvisor) -> None:
        """注册一个顾问实现。"""
        self._advisors.append(advisor)
        if self.log:
            self.log.info(f"已注册 AI 顾问: {advisor.name}")

    def enabled(self) -> bool:
        if self.config is None:
            return True
        return bool(self.config.as_bool("analytics.ai.enabled", True))

    # ---------- 数据 ----------

    def snapshot(self, *, money_trades_limit: int = 20) -> MarketSnapshot:
        """从已采集的指标构造一份快照。"""
        data = self.query.overview()
        return MarketSnapshot(
            taken_at_ms=int(time.time() * 1000),
            m0_minor=int(data.get("m0_minor") or 0),
            minted_minor=int(data.get("minted_minor") or 0),
            treasury_minor=int(data.get("treasury_minor") or 0),
            escrow_minor=int(data.get("escrow_minor") or 0),
            holders=int(data.get("holders") or 0),
            accounts=int(data.get("accounts") or 0),
            avg_balance_minor=int(data.get("avg_balance") or 0),
            median_balance_minor=int(data.get("median_balance") or 0),
            gini=float(data.get("gini_balance") or 0.0),
            top10_share_bps=int(data.get("top10_share_bps") or 0),
            cpi_bps=int(data.get("cpi_bps") or 10000),
            inflation_bps=self.query.inflation_bps(),
            active_shops=int(data.get("active_shops") or 0),
            trade_qty=int(data.get("trade_qty") or 0),
            trade_gross_minor=int(data.get("trade_gross") or 0),
            hot_items=self.query.top_items(limit=10),
            risk_flags=self.query.recent_risk_flags(limit=money_trades_limit),
        )

    # ---------- 分析 ----------

    def analyze(self, snapshot: Optional[MarketSnapshot] = None) -> List[Suggestion]:
        if not self.enabled():
            return []
        snap = snapshot or self.snapshot()
        results: List[Suggestion] = []
        for advisor in self._advisors:
            try:
                if advisor.available():
                    results.extend(advisor.analyze(snap))
            except Exception as e:
                if self.log:
                    self.log.error(f"AI 顾问 {advisor.name} 分析失败: {e}")
        results.sort(key=lambda s: {SEVERITY_CRITICAL: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}
                     .get(s.severity, 3))
        return results
