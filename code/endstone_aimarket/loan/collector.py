"""贷款的定时维护与持续追债。

为什么需要"轮询 + 事件加速"两条路
------------------------------------------------------------------
Endstone 0.11.x **没有**能覆盖"物品进入玩家背包"的事件（`InventoryMoveItemEvent`、
`InventoryClickEvent` 都是 Java Bukkit 的概念，Endstone 从来没有；容器类事件还在
Unreleased 未发布）。唯一相关的是 `PlayerPickupItemEvent`，而合成、开箱取物、
/give、交易、漏斗进包全都漏网。

所以采用：
  * **轮询兜底** —— 定时扫描，只针对处于追债状态的玩家（不是全体在线玩家），
    成本 = 欠债人数 × 背包格数，可以忽略；
  * **事件加速** —— 拾取/进服/重生后立刻扫一次该玩家，让抵债近乎实时。

追债期间背包里的物品本来就应当被全部收走，所以扫描时不需要做差异比对，
只判断背包是否非空即可 —— 这里省掉了一层容易出错的复杂度。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core.constants import LoanStatus
from ..core.money import format_amount
from .service import LoanService


class LoanCollector:
    def __init__(self, plugin, loan_service: LoanService, log=None):
        self.plugin = plugin
        self.loans = loan_service
        self.log = log
        self._last_hourly = 0
        self._notified: Dict[str, int] = {}   # loan_id -> 上次催收时间

    # ==================== 每小时维护 ====================

    def hourly(self, *, now: Optional[int] = None) -> Dict[str, int]:
        """计息 + 违约判定 + 触发追债。每小时执行一次。"""
        current = int(now if now is not None else time.time() * 1000)
        stats = {"accrued_minor": 0, "newly_collecting": 0, "checked": 0}

        result = self.loans.accrue_all(now=current)
        stats["accrued_minor"] = int(result.get("accrued_minor", 0))

        for loan in self.loans.repo.all_open():
            stats["checked"] += 1
            trigger = self.loans.evaluate_default(loan, now=current)
            if trigger is None:
                continue
            if loan.is_collecting:
                continue
            if loan.is_company:
                # 公司违约走破产清算，不没收背包
                self._liquidate_company(loan)
            else:
                self.loans.mark_collecting(loan, trigger)
                stats["newly_collecting"] += 1
                self._notify_player(loan, "loan.notice.collecting", force=True)
        return stats

    # ==================== 持续追债 ====================

    def watch(self) -> int:
        """定时扫描：对处于追债状态的在线玩家执行没收与变卖。返回处理的玩家数。"""
        handled = 0
        for player in self._online_players():
            try:
                if self._collect_from(player):
                    handled += 1
            except Exception as e:
                if self.log:
                    self.log.error(f"追债扫描玩家失败: {e}")
        return handled

    def on_player_event(self, player) -> None:
        """事件加速：拾取/进服/重生后立刻处理一次。"""
        try:
            self._collect_from(player)
        except Exception as e:
            if self.log:
                self.log.error(f"事件追债失败: {e}")

    def _collect_from(self, player) -> bool:
        account = self.plugin.economy_api.api_get_account(
            xuid=_attr(player, "xuid"), name=_attr(player, "name")
        )
        if account is None:
            return False

        loans = [l for l in self.loans.loans_of(account.account_id) if l.is_collecting]
        if not loans:
            # 顺手把结清后该归还的物品交还
            self._deliver_returns(player, account.account_id)
            return False

        # 背包为空直接跳过，省掉一次无意义的 take
        summary = self.plugin.inventory.summary(player)
        if int(summary.get("total_items", 0)) <= 0:
            return False

        acted = False
        for loan in loans:
            result = self.loans.seize(player, loan)
            if not result.ok:
                if self.log:
                    self.log.error(f"追债失败 {loan.loan_id}: {result.message}")
                continue
            report = result.value
            if report.items_taken > 0:
                acted = True
                player.send_message(
                    self.plugin.text(
                        "loan.seize.done",
                        items=report.items_taken,
                        proceeds=format_amount(report.proceeds_minor),
                        repaid=format_amount(report.repaid_minor),
                        left=format_amount(report.remaining_debt_minor),
                    )
                )
                if report.items_stored:
                    player.send_message(
                        self.plugin.text("loan.seize.stored", n=report.items_stored)
                    )
                if report.refunded_minor > 0:
                    player.send_message(
                        self.plugin.text(
                            "loan.seize.refund", value=format_amount(report.refunded_minor)
                        )
                    )
                if report.settled:
                    player.send_message(self.plugin.text("loan.seize.settled"))
        return acted

    def _deliver_returns(self, player, account_id: str) -> int:
        """把结清贷款时标记为可归还的物品真正交还给玩家（实现在 LoanService 里）。"""
        try:
            return self.loans.deliver_returns(player, account_id)
        except Exception as e:
            if self.log:
                self.log.error(f"归还暂存物品失败: {e}")
            return 0

    # ==================== 催收提醒 ====================

    def remind(self, *, now: Optional[int] = None) -> int:
        """对逾期但尚未进入追债的玩家发催收通知，每个贷款每天最多一次。"""
        current = int(now if now is not None else time.time() * 1000)
        sent = 0
        for loan in self.loans.repo.all_open():
            if loan.status not in (LoanStatus.ACTIVE.value, LoanStatus.OVERDUE.value):
                continue
            due = loan.total_due_minor
            if due <= 0:
                continue
            last = self._notified.get(loan.loan_id, 0)
            if current - last < 24 * 3600 * 1000:
                continue
            remaining = loan.due_at_ms - current
            if remaining > 3 * 24 * 3600 * 1000:
                continue
            self._notified[loan.loan_id] = current
            key = "loan.notice.overdue" if remaining < 0 else "loan.notice.due_soon"
            self._notify_player(loan, key)
            sent += 1
        return sent

    def _notify_player(self, loan, key: str, *, force: bool = False) -> bool:
        player = self._find_online(loan.borrower_account_id)
        if player is None:
            return False
        try:
            player.send_message(
                self.plugin.text(
                    key,
                    amount=format_amount(loan.total_due_minor),
                    due=_fmt_time(loan.due_at_ms),
                    grace=_fmt_time(loan.grace_until_ms),
                )
            )
            return True
        except Exception:
            return False

    # ==================== 公司违约 ====================

    def _liquidate_company(self, loan) -> None:
        if self.plugin.company_service is None:
            return
        company = self.plugin.company_service.repo.get_by_account(loan.borrower_account_id)
        if company is None:
            return
        result = self.plugin.company_service.liquidate(company, reason="debt_default")
        if result.ok:
            if self.log:
                self.log.warning(f"公司 {company.name} 因债务违约破产：{result.value.describe()}")
            # 通知发起人（只是通知，不动他的个人资产）
            founder_account = self.plugin.economy_service.repo.get_account(company.founder_account_id)
            name = getattr(founder_account, "current_name", None) if founder_account else None
            if name:
                try:
                    founder = self.plugin.server.get_player(name)
                except Exception:
                    founder = None
                if founder is not None:
                    founder.send_message(
                        self.plugin.text("company.liquidated.notice", name=company.name)
                    )
        elif self.log:
            self.log.error(f"公司 {company.name} 清算失败: {result.message}")

    # ==================== 工具 ====================

    def _online_players(self) -> List[Any]:
        try:
            return list(self.plugin.server.online_players)
        except Exception:
            return []

    def _find_online(self, account_id: str):
        for player in self._online_players():
            account = self.plugin.economy_api.api_get_account(
                xuid=_attr(player, "xuid"), name=_attr(player, "name")
            )
            if account is not None and account.account_id == account_id:
                return player
        return None


def _attr(obj, name: str):
    if obj is None:
        return None
    try:
        value = getattr(obj, name, None)
        return str(value) if value is not None else None
    except Exception:
        return None


def _fmt_time(ms: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except Exception:
        return "-"
