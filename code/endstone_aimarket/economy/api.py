"""经济内核的对外门面。

这是插件内部（以及将来可能的外部插件）唯一应该调用的经济接口。
命名沿用 Endstone 社区习惯的 `api_` 前缀，方便被其它插件发现与调用。
返回值分两类：
  * `api_get_*` 之类的只读接口直接返回数据；
  * 涉及写操作的返回 Result[Receipt]，调用方必须检查 ok。
"""
from __future__ import annotations

from typing import List, Optional

from ..core.money import format_amount, to_unit
from ..core.result import Result
from .models import Account, Balance, Receipt, TxRecord
from .service import EconomyService


class EconomyAPI:
    def __init__(self, service: EconomyService):
        self._svc = service

    @property
    def service(self) -> EconomyService:
        return self._svc

    # ---------- 账户 ----------

    def api_get_account(
        self,
        *,
        account_id: Optional[str] = None,
        xuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Account]:
        result = self._svc.resolve(account_id=account_id, xuid=xuid, name=name)
        return result.value if result.ok else None

    def api_ensure_player(self, player) -> Optional[Account]:
        """传入 Endstone Player，返回其经济账户（不存在则开户）。"""
        result = self._svc.ensure_player(
            xuid=_safe_attr(player, "xuid"),
            player_uuid=_safe_attr(player, "unique_id"),
            name=_safe_attr(player, "name"),
        )
        return result.value if result.ok else None

    # ---------- 余额（只读） ----------

    def api_get_balance_minor(
        self,
        *,
        account_id: Optional[str] = None,
        xuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> int:
        """返回最小单位余额；找不到账户返回 0。"""
        result = self._svc.balance_of(account_id=account_id, xuid=xuid, name=name)
        return result.value.balance_minor if result.ok and result.value else 0

    def api_get_available_minor(
        self,
        *,
        account_id: Optional[str] = None,
        xuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> int:
        result = self._svc.balance_of(account_id=account_id, xuid=xuid, name=name)
        return result.value.available_minor if result.ok and result.value else 0

    def api_get_balance_text(self, player) -> str:
        """给 UI / 变量系统用的格式化余额，如 '1,234.56金'。"""
        account = self.api_ensure_player(player)
        if account is None:
            return format_amount(0)
        balance = self._svc.get_balance(account.account_id)
        return format_amount(balance.balance_minor)

    def api_get_balance_unit(self, *, account_id: str = None, xuid: str = None, name: str = None) -> str:
        return str(to_unit(self.api_get_balance_minor(account_id=account_id, xuid=xuid, name=name)))

    def api_balance(self, account_id: str) -> Balance:
        return self._svc.get_balance(account_id)

    def api_has(self, account_id: str, amount_minor: int) -> bool:
        return self._svc.get_balance(account_id).available_minor >= int(amount_minor)

    # ---------- 写操作 ----------

    def api_deposit(
        self, account_id: str, amount_minor: int, *, reason: str = "api", idem: str = None
    ) -> Result[Receipt]:
        return self._svc.deposit(account_id, amount_minor, reason=reason, idem=idem)

    def api_withdraw(
        self, account_id: str, amount_minor: int, *, reason: str = "api", idem: str = None
    ) -> Result[Receipt]:
        return self._svc.withdraw(account_id, amount_minor, reason=reason, idem=idem)

    def api_transfer(
        self,
        src_account_id: str,
        dst_account_id: str,
        amount_minor: int,
        *,
        reason: str = "api",
        idem: str = None,
    ) -> Result[Receipt]:
        return self._svc.transfer(
            src_account_id, dst_account_id, amount_minor, reason=reason, idem=idem
        )

    def api_transfer_taxed(
        self,
        src_account_id: str,
        dst_account_id: str,
        amount_minor: int,
        *,
        tax_bps: int = 0,
        reason: str = "api",
        idem: str = None,
    ) -> Result[Receipt]:
        return self._svc.transfer_taxed(
            src_account_id,
            dst_account_id,
            amount_minor,
            tax_bps=tax_bps,
            reason=reason,
            idem=idem,
        )

    # ---------- 历史与统计 ----------

    def api_history(self, account_id: str, *, limit: int = 20, offset: int = 0) -> List[TxRecord]:
        return self._svc.history(account_id, limit=limit, offset=offset)

    def api_total_supply_minor(self) -> int:
        """玩家手持货币总量（M0）。"""
        return self._svc.total_supply()

    def api_minted_total_minor(self) -> int:
        return self._svc.minted_total()

    def api_treasury_minor(self) -> int:
        return self._svc.treasury_balance()


def _safe_attr(obj, name: str) -> Optional[str]:
    """安全取属性：Endstone 的 Player 在部分版本/离线场景下属性可能缺失。"""
    try:
        value = getattr(obj, name, None)
        return str(value) if value is not None else None
    except Exception:
        return None
