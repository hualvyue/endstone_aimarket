"""金融监管：报表、风险监测、告警与坏账核销。

央行能印钞，所以它**不会因挤兑而破产** —— 挤兑的代价是超发导致的通胀。
因此本模块监测的是"货币纪律"而不是"偿付能力"：

* **挤兑指数** —— 短期内玩家集中取款的程度
* **超发率** —— 基础货币增速。它不依赖滞后的 CPI：挤兑刚发生时物价还没动，
  但基础货币已经跳了。两者一起看才能判断是"正常流动性需求"还是"恐慌"
* **资本充足率** —— 扩表是否超过了资产支撑
* **通胀偏离** —— 政策目标是否失守

任何一项越线都会**主动通知行长**，并落库 `bank_alerts` 备查。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import LoanStatus
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.money import format_amount
from ..core.result import Result
from .models import Alert
from .repository import DAY_MS, HOUR_MS, now_ms
from .service import CentralBankService, EVENT_RUN

# 告警类型
ALERT_RUN = "run"                # 挤兑
ALERT_CAR = "car"                # 资本充足率跌破
ALERT_INFLATION = "inflation"    # 通胀失控
ALERT_BIG_LOAN = "big_loan"      # 单笔异常放贷
ALERT_RESERVE = "reserve"        # 准备金触顶

DEDUPE_MS = 6 * HOUR_MS          # 同类告警 6 小时内只记一次，避免刷屏


class Regulator:
    def __init__(
        self,
        db: Database,
        bank: CentralBankService,
        *,
        policy_engine=None,
        metrics_query=None,
        config=None,
        log=None,
    ):
        self.db = db
        self.bank = bank
        self.policy = policy_engine
        self.metrics_query = metrics_query
        self.config = config
        self.log = log
        self.repo = bank.repo

    def _cfg_int(self, path: str, default: int) -> int:
        if self.config is None:
            return default
        return int(self.config.as_int(path, default) or default)

    def run_threshold_bps(self) -> int:
        return max(1, self._cfg_int("bank.run_threshold_bps", 2000))

    def big_loan_bps(self) -> int:
        """单笔放贷占总放贷余额多少算"异常大"。0 = 不检查。"""
        return max(0, self._cfg_int("bank.big_loan_bps", 5000))

    # ==================== 报表 ====================

    def snapshot(self) -> Dict[str, Any]:
        sheet = self.bank.balance_sheet()
        policy = self.bank.policy()
        inflation = None
        if self.policy is not None:
            inflation = self.policy.inflation_bps()
        return {
            "资产": sheet.asset_minor,
            "负债": sheet.liability_minor,
            "央行资本": sheet.capital_minor,
            "资本充足率bps": sheet.car_bps,
            "基础货币": sheet.base_money_minor,
            "M0": sheet.m0_minor,
            "M1": sheet.m1_minor,
            "M2": sheet.m2_minor,
            "货币乘数bps": sheet.multiplier_bps,
            "存款总额": sheet.deposit_total_minor,
            "放贷总额": sheet.credit_total_minor,
            "准备金率bps": policy.rrr_bps,
            "可放贷上限": self.bank.reserve_cap_minor(policy),
            "挤兑指数bps": self.bank.run_index_bps(),
            "超发率bps": self.bank.oversupply_bps(),
            "通胀bps": inflation,
            "坏账": self.repo.writeoff_total(),
        }

    def report(self) -> str:
        sheet = self.bank.balance_sheet()
        policy = self.bank.policy()
        lines: List[str] = []
        lines.append(sheet.describe())
        lines.append("")
        lines.append(sheet.money_supply_text())
        lines.append("")
        lines.append(policy.describe())
        lines.append("")
        lines.append(
            f"可放贷上限 {format_amount(self.bank.reserve_cap_minor(policy))}"
            f" ｜ 已放 {format_amount(sheet.credit_total_minor)}"
            f" ｜ 余 {format_amount(self.bank.credit_headroom_minor(policy))}"
        )
        lines.append(
            f"挤兑指数 {self.bank.run_index_bps() / 100:.2f}%"
            f"（阈值 {self.run_threshold_bps() / 100:.2f}%）"
            f" ｜ 超发率 {self.bank.oversupply_bps() / 100:.2f}%"
        )
        lines.append(f"累计坏账核销 {format_amount(self.repo.writeoff_total())}")
        lines.append(f"未处理告警 {self.repo.unacknowledged_count()} 条")
        return "\n".join(lines)

    def alerts_text(self, limit: int = 10) -> str:
        alerts = self.repo.recent_alerts(limit)
        if not alerts:
            return "近期没有任何风险告警"
        lines = []
        for alert in alerts:
            mark = "●" if not alert.acknowledged else "○"
            lines.append(f"{mark} [{alert.severity}] {alert.title}")
            if alert.detail:
                lines.append(f"    {alert.detail}")
        return "\n".join(lines)

    # ==================== 巡检 ====================

    def check(self, *, now: Optional[int] = None) -> List[Alert]:
        """跑一遍全部风控检查。返回本次新产生的告警。"""
        current = int(now if now is not None else now_ms())
        produced: List[str] = []

        # 1) 挤兑
        run_index = self.bank.run_index_bps(now=current)
        if run_index >= self.run_threshold_bps():
            alert_id = self.repo.add_alert(
                kind=ALERT_RUN,
                severity="critical",
                title=f"挤兑预警：短期净取款已达存款的 {run_index / 100:.2f}%",
                detail=(
                    "央行已通过增发满足兑付。注意这不是偿付危机，而是**超发风险** —— "
                    "请关注后续通胀与基础货币增速。可考虑上调再贴现率抑制提款。"
                ),
                metrics={"run_index_bps": run_index, "oversupply_bps": self.bank.oversupply_bps()},
                dedupe_ms=DEDUPE_MS,
            )
            if alert_id:
                produced.append(alert_id)
                self.repo.add_event(
                    kind=EVENT_RUN,
                    amount_minor=0,
                    m0_after_minor=self.bank.m0(),
                    detail=f"挤兑指数 {run_index / 100:.2f}%",
                )

        # 2) 资本充足率
        policy = self.bank.policy()
        car = self.bank.car_bps(policy)
        if car < policy.reserve_floor_car_bps:
            alert_id = self.repo.add_alert(
                kind=ALERT_CAR,
                severity="critical",
                title=f"资本充足率 {car / 100:.2f}% 跌破红线 {policy.reserve_floor_car_bps / 100:.2f}%",
                detail="央行已自动暂停新放贷。可通过核销坏账、减少储备或吸收更多存款来修复。",
                metrics={"car_bps": car, "capital_minor": self.bank.balance_sheet().capital_minor},
                dedupe_ms=DEDUPE_MS,
            )
            if alert_id:
                produced.append(alert_id)

        # 3) 通胀偏离
        if self.policy is not None:
            inflation = self.policy.inflation_bps()
            if inflation is not None:
                gap = abs(inflation - policy.target_inflation_bps)
                if gap > policy.target_band_bps * 3:
                    alert_id = self.repo.add_alert(
                        kind=ALERT_INFLATION,
                        severity="warning",
                        title=f"通胀 {inflation / 100:.2f}% 严重偏离目标 "
                              f"{policy.target_inflation_bps / 100:.2f}%",
                        detail=(
                            "自动调节正在按步长上限逐步修正。"
                            "如需更果断，可手动覆盖基准利率或做一次公开市场操作。"
                        ),
                        metrics={"inflation_bps": inflation, "gap_bps": inflation - policy.target_inflation_bps},
                        dedupe_ms=DEDUPE_MS,
                    )
                    if alert_id:
                        produced.append(alert_id)

        # 4) 准备金触顶
        headroom = self.bank.credit_headroom_minor(policy)
        if headroom <= 0 and self.bank.credit_total_minor() > 0:
            alert_id = self.repo.add_alert(
                kind=ALERT_RESERVE,
                severity="warning",
                title="准备金闸门已触顶，新的放贷会被拒绝",
                detail="可以下调准备金率、或吸收更多存款来扩大可放贷额度。",
                metrics={"cap_minor": self.bank.reserve_cap_minor(policy)},
                dedupe_ms=DEDUPE_MS,
            )
            if alert_id:
                produced.append(alert_id)

        return self.repo.recent_alerts(3) if produced else []

    def check_big_loan(self, amount_minor: int, *, account_id: str = "") -> bool:
        """单笔放贷是否异常大。放款成功后调用，命中就告警。"""
        threshold_bps = self.big_loan_bps()
        if threshold_bps <= 0:
            return False
        sheet = self.bank.balance_sheet()
        base = max(1, sheet.credit_total_minor)
        if amount_minor * 10000 // base < threshold_bps:
            return False
        self.repo.add_alert(
            kind=ALERT_BIG_LOAN,
            severity="warning",
            title=f"异常大额放贷 {format_amount(amount_minor)}",
            detail=f"占当前放贷余额的 {amount_minor * 10000 // base / 100:.2f}%",
            metrics={"amount_minor": amount_minor, "account_id": account_id},
            dedupe_ms=DEDUPE_MS,
        )
        return True

    # ==================== 坏账核销 ====================

    def write_off(self, loan_id: str, *, amount_minor: Optional[int] = None,
                  reason: str = "坏账核销") -> Result[Dict[str, Any]]:
        """把一笔收不回来的贷款核销。

        核销**不需要账本分录** —— 债权不在账本上（loans 表单独记），
        所以只要把状态改掉、记一条核销记录，央行资产自然减少、资本随之下降。
        而当初印出去的货币早就在市面上流通了，不会因为核销而凭空消失。
        """
        row = self.db.query_one("SELECT * FROM loans WHERE loan_id = ?", (loan_id,))
        if row is None:
            return Result.failure(ErrorCode.TX_NOT_FOUND, "找不到这笔贷款")
        outstanding = int(row.get("outstanding_principal_minor", 0)) + int(
            row.get("accrued_interest_minor", 0)
        )
        amount = int(amount_minor) if amount_minor is not None else outstanding
        if amount <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "这笔贷款已经结清了")

        try:
            with self.db.transaction():
                self.db.update(
                    "loans",
                    {
                        "status": LoanStatus.WRITTEN_OFF.value,
                        "outstanding_principal_minor": 0,
                        "accrued_interest_minor": 0,
                        "updated_at_ms": now_ms(),
                    },
                    "loan_id = ?",
                    (loan_id,),
                )
                self.repo.add_writeoff(
                    amount_minor=amount,
                    loan_id=loan_id,
                    borrower_account_id=row.get("borrower_account_id"),
                    reason=reason,
                )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

        capital = self.bank.balance_sheet().capital_minor
        if self.log:
            self.log.warning(
                f"[央行] 核销坏账 {format_amount(amount)}（{loan_id}），"
                f"央行资本降至 {format_amount(capital)}"
            )
        return Result.success({
            "amount_minor": amount,
            "capital_after_minor": capital,
        })

    def auto_write_off_defaulted(self, *, now: Optional[int] = None) -> List[str]:
        """把已经追债处置完毕、确实收不回来的贷款自动核销。

        判定标准：追债动作已经做完（状态 defaulted）且余额仍未清零。
        """
        rows = self.db.query_all(
            "SELECT loan_id, outstanding_principal_minor + accrued_interest_minor AS due "
            "FROM loans WHERE status = ? AND "
            "(outstanding_principal_minor + accrued_interest_minor) > 0",
            (LoanStatus.DEFAULTED.value,),
        )
        done: List[str] = []
        for row in rows:
            result = self.write_off(row["loan_id"], reason="追债终了仍未收回")
            if result.ok:
                done.append(row["loan_id"])
        return done
