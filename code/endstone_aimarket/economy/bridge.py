"""外部货币桥接：兼容 umoney / arc_core / EconomyAPI 等外部经济插件。

模式说明
------------------------------------------------------------------
检测到外部经济插件时，本插件的**内部账本仍是最终权威**（复式记账、
TCC 事务、托管、央行全部照常运行），外部钱包作为它的**实时镜像**：

1. 首次开户：导入玩家在外部经济里的存量余额（老玩家的钱不丢）；
2. 每次内部资金变动：同事务写 `bridge_pending`（应推送的余额），
   事务提交后推送到外部钱包；推送失败由定时任务重试；
3. 玩家上线对账：有待推记录 → 推送内部值（内部赢，杜绝崩溃窗口刷钱）；
   无 → 采纳外部值（外部赢，兼容外部插件的 /pay 等改动）。

外部金额与内部 minor 的换算比由 `unit_minor` 配置
（1 外部单位 = unit_minor 个最小单位，默认 100 = 1 金币）。

适配器
------------------------------------------------------------------
- UMoney：官方 API（api_get_player_money / api_set_player_money）。
- 通用：在配置里直接填外部插件的 get/set 方法名（arc_core / EconomyAPI
  等未公开文档的插件，服主可零代码接入）。
"""
from __future__ import annotations

from typing import Any, List, Optional



class ExternalWallet:
    """外部钱包适配器基类。子类实现 get_ext / set_ext（外部单位）。"""

    name = "external"

    def get_ext(self, player_name: str) -> Optional[int]:
        raise NotImplementedError

    def set_ext(self, player_name: str, value: int) -> bool:
        raise NotImplementedError


class UMoneyWallet(ExternalWallet):
    """UMoney 官方 API。旧版本用 api_reset_player_money 当 set。"""

    name = "umoney"

    def __init__(self, plugin_obj: Any, log=None):
        self.p = plugin_obj
        self.log = log
        self._set_method = (
            "api_set_player_money"
            if hasattr(plugin_obj, "api_set_player_money")
            else "api_reset_player_money"
        )

    def get_ext(self, player_name: str) -> Optional[int]:
        try:
            value = self.p.api_get_player_money(player_name)
            return int(value)
        except Exception as e:
            if self.log:
                self.log.debug(f"umoney 读取余额失败 {player_name}: {e}")
            return None

    def set_ext(self, player_name: str, value: int) -> bool:
        try:
            getattr(self.p, self._set_method)(player_name, int(value))
            return True
        except Exception as e:
            if self.log:
                self.log.warning(f"umoney 写入余额失败 {player_name}: {e}")
            return False


class GenericWallet(ExternalWallet):
    """通用适配：方法名可配置，适用于 arc_core / EconomyAPI 等任意插件。"""

    def __init__(self, plugin_obj: Any, plugin_name: str, *, get_method: str, set_method: str, log=None):
        self.p = plugin_obj
        self.name = plugin_name
        self.log = log
        self._get = get_method
        self._set = set_method

    def get_ext(self, player_name: str) -> Optional[int]:
        try:
            value = getattr(self.p, self._get)(player_name)
            return int(value)
        except Exception as e:
            if self.log:
                self.log.debug(f"{self.name} 读取余额失败 {player_name}: {e}")
            return None

    def set_ext(self, player_name: str, value: int) -> bool:
        try:
            getattr(self.p, self._set)(player_name, int(value))
            return True
        except Exception as e:
            if self.log:
                self.log.warning(f"{self.name} 写入余额失败 {player_name}: {e}")
            return False


# 通用探测的候选方法名（按顺序尝试，配置可覆盖）
_GET_CANDIDATES = ("api_get_player_money", "get_player_money", "get_money", "my_money")
_SET_CANDIDATES = ("api_set_player_money", "set_player_money", "set_money", "api_change_player_money")


def _probe_wallet(plugin_obj: Any, plugin_name: str, log=None) -> Optional[ExternalWallet]:
    """对未知插件做方法名探测；探不到就返回 None。"""
    get_m = next((m for m in _GET_CANDIDATES if callable(getattr(plugin_obj, m, None))), None)
    set_m = next((m for m in _SET_CANDIDATES if callable(getattr(plugin_obj, m, None))), None)
    if get_m and set_m:
        if log:
            log.info(f"外部经济插件 {plugin_name}：get={get_m} set={set_m}")
        return GenericWallet(plugin_obj, plugin_name, get_method=get_m, set_method=set_m, log=log)
    return None


class CurrencyBridge:
    """桥接器：探测外部插件 + 内部/外部金额换算 + 同步原语。"""

    def __init__(self, server=None, log=None, *, priority: Optional[List[str]] = None,
                 plugin_name: str = "", unit_minor: int = 100,
                 methods: Optional[dict] = None):
        self.server = server
        self.log = log
        self.priority = priority or ["umoney", "arc_core", "EconomyAPI"]
        self.plugin_name = (plugin_name or "").strip()
        self.unit_minor = max(1, int(unit_minor))
        self.methods = methods or {}
        self.wallet: Optional[ExternalWallet] = None

    # ---------- 检测 ----------

    def detect(self) -> Optional[str]:
        """按优先级探测外部经济插件。返回适配器名，未检测到返回 None。"""
        # 1) 服主直接指定插件名
        if self.plugin_name:
            obj = self._get_plugin(self.plugin_name)
            if obj is not None:
                get_m = self.methods.get("get") or _GET_CANDIDATES[0]
                set_m = self.methods.get("set") or _SET_CANDIDATES[0]
                if callable(getattr(obj, get_m, None)) and callable(getattr(obj, set_m, None)):
                    self.wallet = GenericWallet(obj, self.plugin_name, get_method=get_m,
                                                set_method=set_m, log=self.log)
                    return self.plugin_name
                if self.log:
                    self.log.warning(
                        f"指定的外部经济插件 {self.plugin_name} 缺少方法 {get_m}/{set_m}，"
                        f"请在 config 的 economy.external.methods 里填正确的方法名"
                    )
        # 2) 按优先级自动探测
        for name in self.priority:
            obj = self._get_plugin(name)
            if obj is None:
                continue
            if name.lower() == "umoney":
                self.wallet = UMoneyWallet(obj, log=self.log)
                return name
            wallet = _probe_wallet(obj, name, log=self.log)
            if wallet is not None:
                self.wallet = wallet
                return name
        return None

    def _get_plugin(self, name: str):
        try:
            return self.server.plugin_manager.get_plugin(name)
        except Exception:
            return None

    @property
    def active(self) -> bool:
        return self.wallet is not None

    # ---------- 换算 ----------

    def to_minor(self, external_value: int) -> int:
        return int(external_value) * self.unit_minor

    def to_external(self, minor: int) -> int:
        """minor -> 外部单位（四舍五入，处理税率等产生的非整单位金额）。"""
        per = self.unit_minor
        # 等价 round-half-up，不引入 float
        return (int(minor) * 2 + per) // (per * 2)

    # ---------- 同步原语 ----------

    def get_minor(self, player_name: str) -> Optional[int]:
        if not self.wallet:
            return None
        value = self.wallet.get_ext(player_name)
        return None if value is None else self.to_minor(value)

    def set_minor(self, player_name: str, minor: int) -> bool:
        if not self.wallet:
            return False
        return self.wallet.set_ext(player_name, self.to_external(minor))
