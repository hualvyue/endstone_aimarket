"""中央银行主体。

**央行 = 三家合一**：
  * 发行科目 `sys:mint`（可为负）—— 印钞、放贷、公开市场买入；负余额就是基础货币净投放
  * 财政科目 `sys:treasury` —— 税收、回收货币
  * 实物储备 `bank_reserves` —— 公开市场操作吞吐的物资

本模块负责央行「作为一家机构」的部分：行长权限、资产负债表、准备金闸门、
资本充足率、挤兑监测。存款 / 信贷 / 货币政策工具 / 理财 / 监管分别在同目录的
deposit / lending / policy / wealth / regulator 里。

**「能印钞」与「税收收归央行」是同一件事的两面**：印钞投放货币、收税回收货币，
两者在央行账上轧差，轧出来的净值就是市面上流通的钱。所以税收在这里不只是财政收入，
它也是最直接的抗通胀工具。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import AccountKind, PolicyLock
from ..core.database import Database
from ..core.errors import ErrorCode
from ..core.ids import normalize_name
from ..core.money import format_amount
from ..core.result import Result
from ..economy.repository import SYSTEM_MINT_ACCOUNT_ID, TREASURY_ACCOUNT_ID
from ..economy.service import EconomyService
from .models import BalanceSheet, Governor, PolicyState
from .repository import BankRepository, DAY_MS, HOUR_MS, now_ms

# 央行的两个科目账户（沿用既有账户，不物理搬钱 —— 账本分录存着当时的余额快照，
# 搬钱会让历史快照对不上、审计立刻报警。语义合并零风险）。
CENTRAL_BANK_ISSUE_ACCOUNT_ID = SYSTEM_MINT_ACCOUNT_ID    # 发行科目
CENTRAL_BANK_FISCAL_ACCOUNT_ID = TREASURY_ACCOUNT_ID      # 财政科目

# 央行事件类型（写进 bank_events，供挤兑与超发监测使用）
EVENT_DEPOSIT = "deposit"        # 存入（货币从流通回到央行）
EVENT_WITHDRAW = "withdraw"      # 支取（货币从央行回到流通）
EVENT_OMO_BUY = "omo_buy"        # 公开市场买入（投放）
EVENT_OMO_SELL = "omo_sell"      # 公开市场卖出（回收）
EVENT_POLICY = "policy"          # 政策调整
EVENT_RUN = "run"                # 挤兑
EVENT_QE = "qe"                  # 量化宽松/紧缩


class CentralBankService:
    def __init__(
        self,
        db: Database,
        economy: EconomyService,
        *,
        config=None,
        log=None,
        i18n=None,
    ):
        self.db = db
        self.economy = economy
        self.config = config
        self.log = log
        self.i18n = i18n
        self.repo = BankRepository(db)

    # ==================== 开关与配置 ====================

    def enabled(self) -> bool:
        if self.config is None:
            return True
        return bool(self.config.get("bank.enabled", True))

    def governor_names(self) -> List[str]:
        if self.config is None:
            return []
        raw = self.config.get("bank.governor_names", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def allow_op_fallback(self) -> bool:
        if self.config is None:
            return True
        return bool(self.config.get("bank.allow_op_fallback", True))

    def reserve_ratio_floor(self) -> int:
        if self.config is None:
            return 0
        return int(self.config.as_int("bank.min_reserve_ratio_bps", 0) or 0)

    # ==================== 行长 ====================

    def sync_governors(self, player=None) -> int:
        """把 config 里的行长名单落到 `bank_governors`。

        config 是**声明式名单**（服主填游戏名），表是**落地态**。
        一旦绑定就只认 XUID —— 以后改名不丢权限，离线也不影响判定。
        """
        names = self.governor_names()
        if not names:
            return 0
        wanted = {normalize_name(n) for n in names}
        bound = 0

        # 1) 当前在线的这位优先：能直接拿到 xuid，不必等账户名索引
        if player is not None:
            pname = normalize_name(str(getattr(player, "name", "") or ""))
            xuid = str(getattr(player, "xuid", "") or "")
            if pname in wanted and xuid:
                account = self.economy.repo.get_by_xuid(xuid)
                if account is None:
                    created = self.economy.ensure_player(
                        xuid=xuid,
                        player_uuid=str(getattr(player, "unique_id", "") or ""),
                        name=getattr(player, "name", None),
                    )
                    account = created.value if created.ok else None
                if account is not None:
                    existing = self.repo.get_governor(account.account_id)
                    if existing is None or existing.revoked_at_ms is not None:
                        self.repo.upsert_governor(
                            account.account_id, xuid, getattr(player, "name", None)
                        )
                        bound += 1

        # 2) 其余名字：从已开户的账户里按名字找
        for name in names:
            account = self.economy.repo.get_by_name(name)
            if account is None or not account.xuid:
                continue
            existing = self.repo.get_governor(account.account_id)
            if existing is not None and existing.revoked_at_ms is None:
                continue
            self.repo.upsert_governor(account.account_id, account.xuid, account.current_name)
            bound += 1

        if bound and self.log:
            self.log.info(f"央行行长名单已同步，新绑定 {bound} 人")
        return bound

    def is_governor_player(self, player) -> bool:
        """只有行长能操作央行。判定只认 XUID。"""
        if not self.enabled():
            return False
        xuid = str(getattr(player, "xuid", "") or "")
        if xuid and self.repo.is_governor_xuid(xuid):
            return True
        # 名单还没落地时（服主没填、或填了但对应玩家还没上过线），
        # 允许 OP 临时代理 —— 否则服主会把自己锁在门外。
        if self.allow_op_fallback() and not self.governor_names():
            return bool(getattr(player, "is_op", False))
        return False

    def is_governor_account(self, account_id: str) -> bool:
        governor = self.repo.get_governor(account_id)
        return governor is not None and governor.is_active

    def list_governors(self) -> List[Governor]:
        return self.repo.list_governors()

    def grant_governor(self, account_id: str, xuid: str, display_name: Optional[str] = None,
                       note: Optional[str] = None) -> Governor:
        return self.repo.upsert_governor(account_id, xuid, display_name, note)

    def revoke_governor(self, account_id: str) -> bool:
        return self.repo.revoke_governor(account_id)

    def governor_hint(self) -> str:
        """给玩家的提示：为什么你现在管不了央行。"""
        if not self.enabled():
            return self.text("bank.hint.disabled", "央行功能未启用")
        if not self.governor_names():
            return self.text(
                "bank.hint.no_config",
                "还没有指定行长 —— 请在 config.json 的 bank.governor_names 里填上你的游戏名",
            )
        if not self.repo.list_governors():
            return self.text(
                "bank.hint.not_bound",
                "名单已填好，但对应玩家还没登录过 —— 用该账号登录一次即可完成绑定",
            )
        return ""

    # ==================== 政策状态 ====================

    def policy(self) -> PolicyState:
        return self.repo.get_policy()

    def save_policy(self, policy: PolicyState) -> None:
        self.repo.save_policy(policy)

    def set_base_rate(self, bps_per_day: int, *, lock: bool = False) -> Result[PolicyState]:
        policy = self.policy()
        policy.base_rate_bps_per_day = max(0, int(bps_per_day))
        if lock:
            policy.lock(PolicyLock.BASE_RATE.value)
        self.repo.save_policy(policy)
        self._log_policy(f"基准利率设为 {policy.base_rate_bps_per_day / 100:.2f}%/天")
        return Result.success(policy)

    def set_reserve_ratio(self, rrr_bps: int, *, lock: bool = False) -> Result[PolicyState]:
        policy = self.policy()
        policy.rrr_bps = max(self.reserve_ratio_floor(), min(10000, int(rrr_bps)))
        if lock:
            policy.lock(PolicyLock.RRR.value)
        self.repo.save_policy(policy)
        self._log_policy(f"存款准备金率设为 {policy.rrr_bps / 100:.2f}%")
        return Result.success(policy)

    def set_inflation_target(self, target_bps: int, *, band_bps: Optional[int] = None,
                             lock: bool = False) -> Result[PolicyState]:
        policy = self.policy()
        policy.target_inflation_bps = int(target_bps)
        if band_bps is not None:
            policy.target_band_bps = max(0, int(band_bps))
        if lock:
            policy.lock(PolicyLock.INFLATION_TARGET.value)
        self.repo.save_policy(policy)
        self._log_policy(f"通胀目标设为 {policy.target_inflation_bps / 100:.2f}%")
        return Result.success(policy)

    def set_spread(self, name: str, bps: int) -> Result[PolicyState]:
        """调整某个产品的利差。基准利率不动，只动这一项。"""
        field = {
            "demand": "demand_spread_bps",
            "time": "term_premium_bps",
            "credit": "credit_spread_bps",
            "overdraft": "od_premium_bps",
            "card": "card_premium_bps",
            "discount": "discount_wedge_bps",
        }.get(str(name).lower())
        if field is None:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "没有这个利率类型")
        policy = self.policy()
        setattr(policy, field, int(bps))
        self.repo.save_policy(policy)
        return Result.success(policy)

    def lock_item(self, key: str) -> Result[PolicyState]:
        policy = self.policy()
        policy.lock(str(key))
        self.repo.save_policy(policy)
        return Result.success(policy)

    def unlock_item(self, key: str) -> Result[PolicyState]:
        policy = self.policy()
        policy.unlock(str(key))
        self.repo.save_policy(policy)
        return Result.success(policy)

    def _log_policy(self, text: str) -> None:
        self.repo.add_event(kind=EVENT_POLICY, detail=text, m0_after_minor=self.m0())
        if self.log:
            self.log.info(f"[央行政策] {text}")

    # ==================== 资产负债表 ====================

    def _outstanding(self, products: List[str]) -> int:
        """按产品汇总央行债权（只算央行放出的，玩家出资的融资不算）。"""
        if not products:
            return 0
        marks = ",".join("?" for _ in products)
        value = self.db.scalar(
            f"SELECT COALESCE(SUM(outstanding_principal_minor + accrued_interest_minor), 0) "
            f"FROM loans WHERE lender_kind = 'bank' AND product IN ({marks}) "
            f"AND status IN ('active','overdue','collecting')",
            tuple(products),
            default=0,
        )
        return int(value or 0)

    def escrow_total(self) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts WHERE kind = ?",
            (AccountKind.ESCROW.value,),
            default=0,
        )
        return int(value or 0)

    def balance_sheet(self) -> BalanceSheet:
        deposits = self.repo.deposit_totals()
        return BalanceSheet(
            reserves_minor=self.repo.reserve_value_total(),
            loans_outstanding_minor=self._outstanding(["loan", "discount"]),
            overdraft_used_minor=self._outstanding(["overdraft"]),
            card_used_minor=self._outstanding(["card"]),
            mortgage_outstanding_minor=self._outstanding(["mortgage"]),
            base_money_minor=self.economy.minted_total(),
            demand_deposits_minor=deposits.get("demand", 0),
            time_deposits_minor=deposits.get("time", 0),
            wealth_deposits_minor=deposits.get("wealth", 0),
            treasury_minor=self.economy.treasury_balance(),
            accrued_interest_minor=self.repo.accrued_interest_total(),
            m0_minor=self.economy.total_supply(),
            escrow_minor=self.escrow_total(),
            deposit_count=self.repo.deposit_account_count(),
            account_count=self._player_account_count(),
        )

    def _player_account_count(self) -> int:
        value = self.db.scalar(
            "SELECT COUNT(*) FROM accounts WHERE kind = ?",
            (AccountKind.PLAYER.value,),
            default=0,
        )
        return int(value or 0)

    def m0(self) -> int:
        return self.economy.total_supply()

    def base_money(self) -> int:
        return self.economy.minted_total()

    def m1(self) -> int:
        deposits = self.repo.deposit_totals()
        return self.m0() + deposits.get("demand", 0)

    def m2(self) -> int:
        deposits = self.repo.deposit_totals()
        return self.m0() + deposits.get("demand", 0) + deposits.get("time", 0) + deposits.get("wealth", 0)

    # ==================== 准备金闸门与资本充足率 ====================

    def deposit_total(self) -> int:
        return sum(self.repo.deposit_totals().values())

    def reserve_cap_minor(self, policy: Optional[PolicyState] = None) -> int:
        """可放贷上限 = 存款总额 × (1 − 准备金率) + 央行自有资产。

        只有央行一家时，货币乘数由"玩家存回央行"形成，准备金率约束的是
        **央行自身的放贷规模**。它不是摆设：存款多少决定了央行还能放多少贷。

        自有资产 = 实物储备 + 财政余额。这部分是央行真实持有的，不受准备金约束。
        """
        p = policy or self.policy()
        deposits = self.deposit_total()
        kept = deposits * max(0, min(10000, p.rrr_bps)) // 10000
        sheet = self.balance_sheet()
        own = max(0, sheet.reserves_minor + sheet.treasury_minor)
        return max(0, deposits - kept + own)

    def credit_total_minor(self) -> int:
        return self.balance_sheet().credit_total_minor

    def credit_headroom_minor(self, policy: Optional[PolicyState] = None) -> int:
        return max(0, self.reserve_cap_minor(policy) - self.credit_total_minor())

    def car_bps(self, policy: Optional[PolicyState] = None) -> int:
        return self.balance_sheet().car_bps

    def car_ok(self, policy: Optional[PolicyState] = None) -> bool:
        """资本充足率是否达标（监管指标，不作为放贷硬闸门）。

        **为什么不用它阻断放贷**：央行能印钞，它的"资本"天然不随放贷增长 ——
        初始投放的货币在会计上就是净资产为负的状态，若拿资本充足率当硬闸门，
        任何一次正常放贷都会把自己锁死。所以放贷的硬闸门交给**准备金率**，
        资本充足率只用于**告警与展示**。
        """
        p = policy or self.policy()
        return self.car_bps(p) >= p.reserve_floor_car_bps

    def can_lend(self, amount_minor: int, *, policy: Optional[PolicyState] = None) -> Result[None]:
        """放贷前的统一闸门：准备金上限。"""
        if amount_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        p = policy or self.policy()
        headroom = self.credit_headroom_minor(p)
        if amount_minor > headroom:
            return Result.failure(
                ErrorCode.BANK_RESERVE_LIMIT,
                f"准备金闸门：最多还能放贷 {format_amount(headroom)}。"
                f"可以下调准备金率、或吸收更多存款来扩大额度",
            )
        return Result.success(None)

    # ==================== 挤兑与超发监测 ====================

    def run_index_bps(self, window_ms: int = HOUR_MS, *, now: Optional[int] = None) -> int:
        """近一段时间的净取款占存款总额的比例（基点）。

        央行能印钞，所以挤兑**不会让它破产** —— 代价是超发导致的通胀。
        因此这个指数必须与 `oversupply_bps` 一起看，不能单独下结论。
        """
        current = int(now if now is not None else now_ms())
        since = current - int(window_ms)
        outflow = int(self.db.scalar(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM bank_events "
            "WHERE kind = ? AND created_at_ms >= ?",
            (EVENT_WITHDRAW, since), default=0) or 0)
        inflow = int(self.db.scalar(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM bank_events "
            "WHERE kind = ? AND created_at_ms >= ?",
            (EVENT_DEPOSIT, since), default=0) or 0)
        deposits = self.deposit_total()
        if deposits <= 0:
            return 0
        net_out = max(0, outflow - inflow)
        return net_out * 10000 // deposits

    def oversupply_bps(self, window_ms: int = DAY_MS, *, now: Optional[int] = None) -> int:
        """基础货币增速（基点）。

        **不依赖滞后的 CPI** —— 挤兑刚发生时物价还没动，但基础货币已经跳了。
        """
        current = int(now if now is not None else now_ms())
        past = self.db.query_one(
            "SELECT value_int FROM metric_samples WHERE metric = 'base_money_minor' "
            "AND bucket_ms <= ? ORDER BY bucket_ms DESC LIMIT 1",
            (current - int(window_ms),),
        )
        if past is None:
            return 0
        old = int(past["value_int"] or 0)
        now_value = self.base_money()
        if old <= 0:
            return 0
        return (now_value - old) * 10000 // old

    def record_flow(self, kind: str, amount_minor: int, *, detail: str = "") -> None:
        """记一笔货币往返事件（存入/支取）。挤兑指数就是用它算的。"""
        if amount_minor <= 0:
            return
        self.repo.add_event(
            kind=kind, amount_minor=int(amount_minor), m0_after_minor=self.m0(), detail=detail
        )

    # ==================== 文案 ====================

    def text(self, key: str, default: str = "", **kwargs) -> str:
        if self.i18n is None:
            return default or key
        return self.i18n.t(key, **kwargs)
