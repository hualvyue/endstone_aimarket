"""中央银行：货币发行、存款、信贷、货币政策与金融监管。

**央行 = 三家合一**（发行 + 财政 + 储备），一家直营全部银行业务，不设独立商业银行。

分层：
    core ← economy / loan ← bank ← aimarket

  * `service`   —— 行长权限、资产负债表、准备金闸门、资本充足率、挤兑监测
  * `deposit`   —— 活期与定期存款（按小时单利计息、时间桶幂等）
  * `lending`   —— 透支 / 信用卡 / 按揭 / 再贴现（全部复用贷款模块的计息与追债）
  * `policy`    —— 基准利率、准备金率、公开市场操作、量化宽松、通胀目标自动调节
  * `wealth`    —— 理财（只落端口，策略接口留给 AI）
  * `regulator` —— 监管报表、风险告警、坏账核销
  * `collector` —— 货币口径指标采集
  * `forms` / `commands` —— 界面与命令
"""
from .collector import BankMetricsCollector
from .commands import BANK_COMMANDS, BANK_PERMISSIONS, BankCommands
from .deposit import DepositService
from .forms import BankForms
from .lending import LendingService
from .models import (
    Alert,
    BalanceSheet,
    Card,
    CreditLine,
    Deposit,
    Governor,
    Mortgage,
    MortgageSchedule,
    OmoRecord,
    PolicyState,
    Reserve,
    Statement,
)
from .policy import PolicyEngine
from .regulator import Regulator
from .repository import BankRepository
from .service import (
    CENTRAL_BANK_FISCAL_ACCOUNT_ID,
    CENTRAL_BANK_ISSUE_ACCOUNT_ID,
    EVENT_DEPOSIT,
    EVENT_OMO_BUY,
    EVENT_OMO_SELL,
    EVENT_POLICY,
    EVENT_QE,
    EVENT_RUN,
    EVENT_WITHDRAW,
    CentralBankService,
)
from .wealth import FixedRateStrategy, WealthService, WealthStrategy

__all__ = [
    # 服务
    "CentralBankService",
    "DepositService",
    "LendingService",
    "PolicyEngine",
    "WealthService",
    "Regulator",
    "BankRepository",
    "BankMetricsCollector",
    "BankForms",
    "BankCommands",
    "BANK_COMMANDS",
    "BANK_PERMISSIONS",
    # 策略
    "WealthStrategy",
    "FixedRateStrategy",
    # 模型
    "BalanceSheet",
    "PolicyState",
    "Deposit",
    "CreditLine",
    "Card",
    "Statement",
    "Mortgage",
    "MortgageSchedule",
    "Reserve",
    "OmoRecord",
    "Alert",
    "Governor",
    # 常量
    "CENTRAL_BANK_ISSUE_ACCOUNT_ID",
    "CENTRAL_BANK_FISCAL_ACCOUNT_ID",
    "EVENT_DEPOSIT",
    "EVENT_WITHDRAW",
    "EVENT_OMO_BUY",
    "EVENT_OMO_SELL",
    "EVENT_POLICY",
    "EVENT_QE",
    "EVENT_RUN",
]
