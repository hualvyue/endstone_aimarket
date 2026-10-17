"""aimarket 插件主入口：装配各层服务、注册事件与定时任务。

分层依赖是单向的：
     core  ←  economy / inventory / market / analytics  ←  tx  ←  shop  ←  aimarket
上层可以调下层，下层永远不反向依赖。会话状态（正在建店的玩家）与 UI 入口
都收在这里，避免散落到各个业务类里。
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from endstone.plugin import Plugin

from .analytics import AIGateway, MetricsCollector, MetricsQuery
from .bank import (
    BANK_COMMANDS,
    BANK_PERMISSIONS,
    BankCommands,
    BankForms,
    BankMetricsCollector,
    CentralBankService,
    DepositService,
    LendingService,
    PolicyEngine,
    Regulator,
    WealthService,
)
from .company import CompanyService
from .core.config import Config, load_json_resource
from .core.constants import (
    AccountKind,
    PricingMode,
    ShopStatus,
    ShopType,
    TradeDirection,
    TxKind,
)
from .core.database import Database
from .core.errors import CODE_LANG_KEY, ErrorCode
from .core.i18n import I18n
from .core.logging import Log
from .core.money import configure_currency, currency_name, currency_symbol, format_amount, minor_per_unit
from .core.result import Result
from .economy import EconomyAPI, EconomyService
from .economy.bridge import CurrencyBridge
from .fundraising import FundraisingService
from .economy.repository import TREASURY_ACCOUNT_ID
from .inventory import InventoryService
from .loan import LoanCollector, LoanService
from .market import PricingEngine
from .shop import (
    COMMANDS,
    PERMISSIONS,
    ShopCommands,
    ShopForms,
    ShopListener,
    ShopPricing,
    ShopRepository,
    ShopService,
)
from .stock import StockForms, StockService
from .tx import TradeCoordinator, TradeRecovery
from .ui import UIForms, UIService
from .auction import (
    AuctionCommands,
    AuctionForms,
    AuctionService,
    COMMANDS as AUCTION_COMMANDS,
    PERMISSIONS as AUCTION_PERMISSIONS,
)

DEFAULT_CONFIG = {
    "economy": {
        "initial_balance_minor": 0,     # 新玩家开户赠送（最小单位，100 = 1 金币）
        "tax_bps": 500,                 # 商店交易税，500 = 5%
        "transfer_tax_bps": 0,          # 玩家间转账税
        "currency_name": "金币",        # 货币名称（服主自定义，如"钻石""铜板"）
        "currency_symbol": "金",        # 金额后缀符号（展示用）
        "minor_per_unit": 100,          # 1 显示单位 = 多少最小单位
        "external": {                   # 外部经济桥接：检测到即接入，检测不到用内置系统
            "enabled": True,
            "priority": ["umoney", "arc_core", "EconomyAPI"],
            "plugin": "",               # 强制指定外部经济插件名（留空则按 priority 自动探测）
            "unit_minor": 100,          # 1 外部货币单位 = 多少最小单位
            "methods": {"get": "", "set": ""},  # 外部插件的取款/存款方法名（通用适配）
        },
    },
    "shop": {
        "max_per_player": 10,           # 每人最多几个店，0 = 不限
        "open_price_minor": 100000,     # 个人开店牌照费（不可退）。公司开店免费
    },
    "company": {
        "min_capital_minor": 100000,    # 最低实缴注册资本
        "max_per_player": 1,            # 每人最多创办几家公司
        "turnover_tax_bps": 800,        # 公司营业额税率（基点），比个人交易税高
        "dividend_bps": 3000,           # 默认分红率：可分配利润拿出 30%
        "reserve_minor": 0,             # 必须保留的运营金，不参与分红
        "liquidation_fee_bps": 0,       # 清算费用比例
    },
    "loan": {
        "personal_base_limit_minor": 100000,   # 个人贷款基础额度
        "company_nav_multiplier_bps": 5000,    # 公司额度 = 净资产 × 50%
        "personal_rate_bps_per_day": 50,       # 个人日息 0.5%
        "company_rate_bps_per_day": 30,        # 公司日息 0.3%
        "default_term_days": 7,
        "grace_days": 3,                        # 宽限期
        "seize_threshold_minor": 500000,        # 欠款达到这个数就直接追债
        "max_total_outstanding_minor": 100000000,  # 全服未偿总额上限（给增发踩刹车）
        "watch_ticks": 20,                      # 追债扫描间隔
        "hourly_ticks": 72000,                  # 计息与违约判定间隔
    },
    "stock": {
        "rand_bps": 500,                # 股价随机指数 ±5%
        "limit_bps": 2000,              # 单周期涨跌停 ±20%
        "min_capital_minor": 100000,    # 上市门槛
        "min_issue_price_minor": 100,   # 最低发行价
        "dividend_tax_bps": 500,        # 股息税
        "pricing_ticks": 72000,         # 定价周期（1 小时）
    },
    "fundraising": {
        "min_amount_minor": 10000,          # 单次出资下限
        "per_funder_limit_minor": 0,        # 同一玩家对同一公司的累计出资上限，0 = 不限
        "cooldown_hours": 24,               # 两次募资的间隔小时数，0 = 不限
        "nav_multiplier_bps": 10000,        # 累计募资 ≤ 净资产 × 100%，0 = 不限
        "lock_days": 3,                     # 募资到账后禁止分红的锁定期，0 = 不限
        "sponsor_min_days": 1,              # 最短赞助天数
    },
    "bank": {
        "enabled": True,
        # ┌──────────────────────────────────────────────────────────────┐
        # │ 央行行长：把你自己的游戏名填在下面这个列表里。                │
        # │ 首次识别后会自动绑定 XUID，所以你以后改名也不会丢权限。       │
        # └──────────────────────────────────────────────────────────────┘
        "governor_names": [],
        "allow_op_fallback": True,          # 名单为空时允许 OP 临时代理

        # —— 存款 ——
        "min_deposit_minor": 100,           # 单笔存款下限
        "max_deposit_minor": 0,             # 单笔存款上限，0 = 不限
        "min_time_days": 1,
        "max_time_days": 365,
        "early_withdraw_penalty_bps": 3000, # 定期提前支取违约金

        # —— 透支（授信额度 + 自动放款）——
        "auto_overdraft": True,             # 余额不足时自动用额度补足
        "overdraft_base_limit_minor": 100000,
        "overdraft_balance_multiplier_bps": 0,   # 额度还可与余额挂钩，0 = 不挂钩
        "overdraft_term_days": 7,

        # —— 信用卡 ——
        "card_base_limit_minor": 300000,
        "card_balance_multiplier_bps": 0,
        "card_term_days": 30,
        "card_cycle_days": 7,               # 账单周期
        "card_grace_days": 7,               # 免息天数
        "card_min_pay_bps": 1000,           # 最低还款比例

        # —— 按揭 / 再贴现（抵押物都是实体店）——
        "mortgage_haircut_bps": 3000,       # 按揭抵押折扣 30%
        "mortgage_max_periods": 12,
        "discount_haircut_bps": 4000,       # 再贴现抵押折扣 40%（更保守）
        "discount_term_days": 7,

        # —— 货币政策 ——
        "omo_spread_bps": 500,              # 公开市场买卖价差，防套利
        "qe_cooldown_hours": 6,             # 量化宽松冷却
        "min_reserve_ratio_bps": 0,         # 准备金率下限

        # —— 监管 ——
        "run_threshold_bps": 2000,          # 挤兑指数告警阈值
        "big_loan_bps": 5000,               # 单笔放贷占总余额多少算异常

        # —— 定时任务 ——
        "accrual_ticks": 72000,             # 1 小时：存款计息
        "monitor_ticks": 12000,             # 10 分钟：监管巡检
        "policy_ticks": 72000,              # 1 小时：自动调节利率
        "maturity_ticks": 1200,             # 1 分钟：定期到期与理财结算
    },
    "ui": {
        "default_carrier": "sidebar",
        "default_items": ["balance", "debt", "portfolio"],
        "refresh_ticks": 40,            # 2 秒刷新一次
    },
    "market": {
        "dynamic": {
            "enabled": True,
            "step_amount_minor": 1_000_000,
            "step_bps": 100,
            "max_buy_increase_bps": 5000,
            "max_sell_decrease_bps": 3000,
            "sell_buy_link_ratio_bps": 5000,
            "recovery_bps_per_hour": 2,
        },
        "daily": {
            "enabled": True,
            "item_count": 3,
            "min_bps": -1500,
            "max_bps": 1500,
            "reset_hour": 0,
        },
    },
    "analytics": {
        "enabled": True,
        "collect_interval_minutes": 60,
        "keep_days": 180,
        "ai": {"enabled": True},
    },
    "tasks": {
        "collect_ticks": 72000,   # 1 小时
        "recovery_ticks": 72000,  # 1 小时
        "cleanup_ticks": 24000,   # 20 分钟检查一次
    },
    "auction": {
        "enabled": True,
        "fee_bps": 200,              # 拍卖手续费费率：保留价 × 2%（进国库）
        "min_fee_minor": 1000,       # 单场最低手续费（1 金币）
        "max_qty_per_listing": 1000, # 单次上架数量上限
        "offline_seconds": 86400,    # 离线拍默认时长：24 小时
        "live_seconds": 60,          # 现场拍默认时长：60 秒
        "live_extend_ms": 10000,     # 现场拍防狙击：临近结束出价延长 10 秒
        "live_max_seconds": 300,     # 现场拍总时长上限：5 分钟
    },
}

TICKS_PER_HOUR = 72000


class AimarketPlugin(Plugin):
    prefix = "Aimarket"
    version = "1.4.2"
    api_version = "0.11"
    load = "POSTWORLD"
    description = "一体化经济内核与按钮商店（自建经济系统，无第三方依赖）"

    commands = {**COMMANDS, **BANK_COMMANDS, **AUCTION_COMMANDS}
    permissions = {**PERMISSIONS, **BANK_PERMISSIONS, **AUCTION_PERMISSIONS}

    # ==================== 生命周期 ====================

    def __init__(self):
        super().__init__()
        self.log = Log("Aimarket")
        self.db: Optional[Database] = None
        self.cfg: Optional[Config] = None
        self.i18n: Optional[I18n] = None
        self.price_table: Dict[str, Any] = {}

        self.economy_service: Optional[EconomyService] = None
        self.bridge = None
        self.command_name_map: Dict[str, str] = {}
        self.economy_api: Optional[EconomyAPI] = None
        self.inventory: Optional[InventoryService] = None
        self.pricing: Optional[PricingEngine] = None
        self.coordinator: Optional[TradeCoordinator] = None
        self.recovery: Optional[TradeRecovery] = None
        self.shop_repo: Optional[ShopRepository] = None
        self.shop_service: Optional[ShopService] = None
        self.shop_pricing: Optional[ShopPricing] = None
        self.forms: Optional[ShopForms] = None
        self.commands_handler: Optional[ShopCommands] = None
        self.metrics: Optional[MetricsCollector] = None
        self.metrics_query: Optional[MetricsQuery] = None
        self.ai: Optional[AIGateway] = None

        # 二期新增
        self.company_service: Optional[CompanyService] = None
        self.loan_service: Optional[LoanService] = None
        self.loan_collector: Optional[LoanCollector] = None
        self.stock_service: Optional[StockService] = None
        self.stock_forms: Optional[StockForms] = None
        self.fundraising_service: Optional[FundraisingService] = None
        self.ui_service: Optional[UIService] = None
        self.ui_forms: Optional[UIForms] = None

        # 三期新增：中央银行
        self.bank_service: Optional[CentralBankService] = None
        self.bank_deposit: Optional[DepositService] = None
        self.bank_lending: Optional[LendingService] = None
        self.bank_wealth: Optional[WealthService] = None
        self.bank_policy: Optional[PolicyEngine] = None
        self.bank_regulator: Optional[Regulator] = None
        self.bank_metrics: Optional[BankMetricsCollector] = None
        self.bank_forms: Optional[BankForms] = None
        self.bank_commands: Optional[BankCommands] = None

        # 拍卖（现场拍 / 离线拍）
        self.auction_service: Optional[AuctionService] = None
        self.auction_forms: Optional[AuctionForms] = None
        self.auction_commands: Optional[AuctionCommands] = None

        self._building: Dict[str, Dict[str, Any]] = {}
        self._tasks: list = []
        self._last_cleanup_day = None

    def on_load(self) -> None:
        self.log.bind(getattr(self, "logger", None))
        self.log.info("正在装载 aimarket ...")
        # 命令冲突自适应：必须在装配前执行（endstone 在启用阶段才注册命令）
        self._deconflict_commands()
        data_dir = Path(str(self.data_folder))
        data_dir.mkdir(parents=True, exist_ok=True)

        # ---------- 配置与文案 ----------
        self.cfg = Config(data_dir / "config.json", DEFAULT_CONFIG, self.log)
        self.i18n = I18n(data_dir / "lang", "zh_CN", self.log)

        # 货币自定义：名称 / 符号 / 换算比（全项目运行时生效）
        econ_cfg = self.cfg.section("economy")
        configure_currency(
            name=econ_cfg.get("currency_name"),
            symbol=econ_cfg.get("currency_symbol"),
            per_unit=econ_cfg.get("minor_per_unit"),
        )
        self.price_table = load_json_resource(
            data_dir / "prices.json", _bundled_price_table(), self.log
        ) or _bundled_price_table()

        # ---------- 存储 ----------
        self.db = Database(data_dir / "aimarket.db", self.log)
        self._migrate()

        # ---------- 服务装配 ----------
        initial = int(self.cfg.as_int("economy.initial_balance_minor", 0))
        self.economy_service = EconomyService(self.db, self.log, initial_balance_minor=initial)
        self.economy_api = EconomyAPI(self.economy_service)
        self.inventory = InventoryService(self.log)

        self.pricing = PricingEngine(self.db, self.cfg, self.log)
        self.pricing.load_price_table(self.price_table)

        # 公司：独立经济主体（账户 kind='company'，数据库层面禁负 → 有限责任）
        self.company_service = CompanyService(
            self.db, self.economy_service, self.pricing, config=self.cfg, log=self.log
        )

        self.coordinator = TradeCoordinator(
            self.db, self.economy_service, self.inventory, self.pricing,
            companies=self.company_service, config=self.cfg, log=self.log,
        )
        self.recovery = TradeRecovery(self.db, self.economy_service, self.log)

        self.shop_repo = ShopRepository(self.db)
        self.shop_pricing = ShopPricing(self.pricing, self.cfg, self.log)
        self.shop_service = ShopService(
            self.db, self.economy_service, self.inventory, self.shop_pricing,
            companies=self.company_service, config=self.cfg, i18n=self.i18n, log=self.log,
        )
        self.forms = ShopForms(self)
        self.commands_handler = ShopCommands(self)

        # 贷款与追债
        self.loan_service = LoanService(
            self.db, self.economy_service, self.inventory, self.pricing,
            self.company_service, config=self.cfg, log=self.log,
        )
        self.loan_collector = LoanCollector(self, self.loan_service, self.log)

        # 股票（系统做市）
        self.stock_service = StockService(
            self.db, self.economy_service, self.company_service,
            config=self.cfg, log=self.log,
        )

        self.stock_forms = StockForms(self)

        # 公司募资：投资（换股）/ 融资（换债权）/ 赞助（换广告位）
        self.fundraising_service = FundraisingService(
            self.db, self.economy_service, self.company_service,
            self.loan_service, self.stock_service,
            config=self.cfg, log=self.log,
        )

        # 玩家自选显示
        self.ui_service = UIService(self, self.db, config=self.cfg, log=self.log)
        self.ui_forms = UIForms(self)

        self.metrics = MetricsCollector(
            self.db, self.economy_service, self.pricing, self.shop_repo, self.log
        )
        self.metrics_query = MetricsQuery(self.db)
        self.ai = AIGateway(self.metrics_query, self.cfg, self.log)

        # ---------- 中央银行（发行 + 财政 + 储备，三家合一，一家直营）----------
        self.bank_service = CentralBankService(
            self.db, self.economy_service, config=self.cfg, log=self.log, i18n=self.i18n
        )
        self.bank_deposit = DepositService(
            self.db, self.economy_service, self.bank_service, config=self.cfg, log=self.log
        )
        self.bank_lending = LendingService(
            self.db, self.economy_service, self.bank_service, self.loan_service,
            inventory=self.inventory, pricing=self.pricing, config=self.cfg, log=self.log,
        )
        self.bank_wealth = WealthService(
            self.db, self.economy_service, self.bank_service, self.bank_deposit,
            config=self.cfg, log=self.log,
        )
        self.bank_policy = PolicyEngine(
            self.db, self.economy_service, self.bank_service,
            inventory=self.inventory, pricing=self.pricing,
            metrics_query=self.metrics_query, config=self.cfg, log=self.log,
        )
        self.bank_regulator = Regulator(
            self.db, self.bank_service, policy_engine=self.bank_policy,
            metrics_query=self.metrics_query, config=self.cfg, log=self.log,
        )
        self.bank_metrics = BankMetricsCollector(
            self.db, self.bank_service, policy_engine=self.bank_policy,
            metrics_query=self.metrics_query, log=self.log,
        )
        self.bank_forms = BankForms(self)
        self.bank_commands = BankCommands(self)

        # ---------- 拍卖 ----------
        self.auction_service = AuctionService(
            self.db, self.economy_service, self.inventory,
            config=self.cfg, i18n=self.i18n, log=self.log,
            resolve_player=self._player_by_account,
        )
        self.auction_forms = AuctionForms(self)
        self.auction_commands = AuctionCommands(self)

        self.log.info(
            f"装载完成：价格表 {len(self.pricing.rules())} 项，"
            f"库存表已就绪，货币 {currency_name()}"
        )

    def _setup_currency_bridge(self) -> None:
        """探测外部经济插件（umoney / arc_core / EconomyAPI…）。

        检测到 → 接入桥接（内部账本为权威，外部钱包实时镜像）；
        检测不到 → 使用内置货币系统。两种模式的业务代码完全一致。
        """
        self.bridge = None
        try:
            ext = self.cfg.section("economy").get("external") or {}
            if not ext.get("enabled", True):
                self.log.info("外部经济桥接已关闭（economy.external.enabled=false），使用内置货币系统")
                return
            bridge = CurrencyBridge(
                self.server, self.log,
                priority=ext.get("priority"),
                plugin_name=ext.get("plugin") or "",
                unit_minor=ext.get("unit_minor", minor_per_unit()),
                methods=ext.get("methods"),
            )
            provider = bridge.detect()
            if provider:
                self.economy_service.attach_bridge(bridge)
                self.bridge = bridge
                self.log.info(
                    f"已接入外部经济插件 {provider}：内部账本为权威，外部钱包实时镜像"
                    f"（1 外部单位 = {bridge.unit_minor} minor）；"
                    f"外部插件的 /pay 等改动会在玩家上线对账时采纳"
                )
            else:
                self.log.info("未检测到外部经济插件，使用内置货币系统")
        except Exception as e:
            self.log.warning(f"外部经济桥接探测失败（继续使用内置货币系统）: {e}")
        self.log.info(
            f"货币：{currency_name()}（符号 {currency_symbol()}，1 单位 = {minor_per_unit()} minor）"
        )

    def on_enable(self) -> None:
        self.log.bind(getattr(self, "logger", None))
        try:
            self.economy_service.bootstrap()
            self._setup_currency_bridge()
            self.pricing.ensure_states()
            # 央行：落一行默认政策、写入内置理财产品
            self.bank_service.repo.get_policy()
            self.bank_wealth.ensure_products()
            # 名单已填、且该玩家已开户的话，启动时就能绑定行长
            self.bank_service.sync_governors()
        except Exception as e:
            self.log.error(f"初始化经济内核失败: {e}")

        # 崩溃恢复：处理上次非正常退出留下的半截交易
        try:
            stats = self.recovery.scan_and_recover()
            if stats.get("applying_manual"):
                self.log.warning(
                    f"有 {stats['applying_manual']} 笔交易状态不明，已冻结等待管理员用 "
                    f"/shopadmin resolve 裁决"
                )
        except Exception as e:
            self.log.error(f"崩溃恢复失败: {e}")

        # 事件
        try:
            self.register_events(ShopListener(self))
            self.register_events(JoinListener(self))
            self.register_events(BusinessListener(self))
        except Exception as e:
            self.log.error(f"注册事件失败: {e}")

        self._schedule_tasks()

        # 启用时先采一次指标，避免管理面板一开始全是空
        try:
            self.metrics.collect()
        except Exception as e:
            self.log.error(f"首次指标采集失败: {e}")

        self.log.info("aimarket 已启用")

    def on_disable(self) -> None:
        try:
            self.server.scheduler.cancel_tasks(self)
        except Exception as e:
            self.log.warning(f"取消定时任务时出错: {e}")
        self._tasks.clear()
        self._building.clear()
        if self.db is not None:
            self.db.close()
        self.log.info("aimarket 已卸载")

    def on_command(self, sender, command, args) -> bool:
        try:
            # 冲突改名后的命令 → 原始处理分支
            name = self.command_name_map.get(command.name, command.name)
            if name == "bank":
                if self.bank_commands is None:
                    return False
                return self.bank_commands.handle(sender, command, args)
            if name == "auction":
                if self.auction_commands is None:
                    return False
                return self.auction_commands.handle(sender, command, args)
            if self.commands_handler is None:
                return False
            return self.commands_handler.handle(sender, command, args)
        except Exception as e:
            self.log.error(f"命令处理异常: {e}")
            return False

    # ==================== 命令冲突自适应 ====================

    def _deconflict_commands(self) -> None:
        """其他插件占用了我们的命令名时，自动换名注册。

        endstone 在启用阶段才读取 commands 注册命令，所以 on_load 里改名
        可以赶在注册之前生效。改名规则：空闲的别名升为主名；都没有则加
        `aimarket_` 前缀。分发用 command_name_map 映射回原始处理分支。
        """
        self.command_name_map: Dict[str, str] = {}
        try:
            probe = self.server.get_plugin_command("__aimarket_probe__")
        except Exception:
            return  # server 尚未注入：跳过（保持原始命令名）
        del probe
        new_commands = {}
        for name, cmd in self.commands.items():
            taken = False
            try:
                taken = self.server.get_plugin_command(name) is not None
            except Exception:
                pass
            if not taken:
                new_commands[name] = cmd
                continue
            cmd = dict(cmd)
            aliases = [a for a in (cmd.get("aliases") or []) if not self._name_taken(a)]
            if aliases and aliases[0] not in new_commands:
                new_name = aliases[0]
                cmd["aliases"] = [a for a in (cmd.get("aliases") or []) if a != new_name]
            else:
                new_name = f"aimarket_{name}"
            new_commands[new_name] = cmd
            self.command_name_map[new_name] = name
            self.log.warning(f"命令 /{name} 已被其他插件占用，自动改用 /{new_name}")
        self.commands = new_commands

    def _name_taken(self, name: str) -> bool:
        try:
            return self.server.get_plugin_command(name) is not None
        except Exception:
            return False

    # ==================== 初始化细节 ====================

    def _migrate(self) -> None:
        """按文件名顺序应用 migrations 目录下的 SQL。"""
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.exists():
            self.log.warning("未找到 migrations 目录，跳过建表")
            return
        self.db.ensure_migrations_table()
        files = sorted(migrations_dir.glob("*.sql"))
        for index, path in enumerate(files, start=1):
            version = int(path.name.split("_", 1)[0]) if path.name.split("_", 1)[0].isdigit() else index
            try:
                script = path.read_text(encoding="utf-8")
            except Exception as e:
                self.log.error(f"读取迁移 {path.name} 失败: {e}")
                continue
            try:
                self.db.apply_migration(version, path.name, script)
            except Exception as e:
                self.log.error(f"应用迁移 {path.name} 失败: {e}")

    def _schedule_tasks(self) -> None:
        scheduler = self.server.scheduler
        tasks_cfg = self.cfg.section("tasks")
        collect_ticks = int(tasks_cfg.get("collect_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        recovery_ticks = int(tasks_cfg.get("recovery_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        cleanup_ticks = int(tasks_cfg.get("cleanup_ticks", 24000) or 24000)

        loan_cfg = self.cfg.section("loan")
        hourly_ticks = int(loan_cfg.get("hourly_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        watch_ticks = max(20, int(loan_cfg.get("watch_ticks", 20) or 20))
        stock_cfg = self.cfg.section("stock")
        pricing_ticks = int(stock_cfg.get("pricing_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        ui_ticks = max(20, int(self.cfg.as_int("ui.refresh_ticks", 40) or 40))

        self._tasks.append(scheduler.run_task(self, self._task_collect, 200, collect_ticks))
        self._tasks.append(scheduler.run_task(self, self._task_market, 400, recovery_ticks))
        self._tasks.append(scheduler.run_task(self, self._task_cleanup, 1200, cleanup_ticks))
        # 计息 + 违约判定
        self._tasks.append(scheduler.run_task(self, self._task_loan_hourly, 600, hourly_ticks))
        # 持续追债：只扫处于追债状态的玩家，不是全体在线玩家
        self._tasks.append(scheduler.run_task(self, self._task_loan_watch, 100, watch_ticks))
        # 股价定价周期
        self._tasks.append(scheduler.run_task(self, self._task_stock_price, 800, pricing_ticks))
        # 显示刷新：只刷配置了显示内容的在线玩家
        self._tasks.append(scheduler.run_task(self, self._task_ui_refresh, 300, ui_ticks))

        # 央行四条任务：计息 / 监管巡检 / 政策自动调节 / 到期处理
        bank_cfg = self.cfg.section("bank")
        accrual_ticks = int(bank_cfg.get("accrual_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        monitor_ticks = max(20, int(bank_cfg.get("monitor_ticks", 12000) or 12000))
        policy_ticks = int(bank_cfg.get("policy_ticks", TICKS_PER_HOUR) or TICKS_PER_HOUR)
        maturity_ticks = max(20, int(bank_cfg.get("maturity_ticks", 1200) or 1200))
        self._tasks.append(scheduler.run_task(self, self._task_bank_accrual, 700, accrual_ticks))
        self._tasks.append(scheduler.run_task(self, self._task_bank_monitor, 900, monitor_ticks))
        self._tasks.append(scheduler.run_task(self, self._task_bank_policy, 1100, policy_ticks))
        self._tasks.append(scheduler.run_task(self, self._task_bank_maturity, 500, maturity_ticks))

        # 拍卖结算：高频扫描到期拍卖（现场拍秒级、离线拍到点），批量结算
        self._tasks.append(scheduler.run_task(self, self._task_auction_settle, 1300, 100))

        # 外部货币桥接：重试待推送的余额同步（崩溃/失败后的兜底）
        if self.bridge is not None:
            self._tasks.append(scheduler.run_task(self, self._task_bridge_push, 1400, 100))

        self.log.info(f"已注册 {len(self._tasks)} 个定时任务")

    def _task_bridge_push(self) -> None:
        try:
            pushed = self.economy_service.push_pending()
            if pushed:
                self.log.debug(f"桥接同步：已推送 {pushed} 条待同步余额")
        except Exception as e:
            self.log.debug(f"桥接同步失败: {e}")

    # ==================== 定时任务 ====================

    def _task_collect(self) -> None:
        try:
            if self.cfg.as_bool("analytics.enabled", True):
                self.metrics.collect()
                # 央行货币口径指标：base_money_minor 是挤兑监测的基准序列
                self.bank_metrics.collect()
        except Exception as e:
            self.log.error(f"指标采集任务失败: {e}")

    def _task_market(self) -> None:
        try:
            self.pricing.maybe_apply_daily()
            self.pricing.apply_recovery()
        except Exception as e:
            self.log.error(f"市场维护任务失败: {e}")

    def _task_cleanup(self) -> None:
        try:
            today = time.strftime("%Y-%m-%d")
            if self._last_cleanup_day == today:
                return
            self._last_cleanup_day = today
            keep = int(self.cfg.as_int("analytics.keep_days", 180))
            self.metrics.cleanup(keep_days=keep)
            self.metrics.cleanup_market_history(keep_days=max(30, keep // 2))
            self.recovery.release_stale_freezes()
            if self.fundraising_service is not None:
                self.fundraising_service.expire_sponsorships()
        except Exception as e:
            self.log.error(f"清理任务失败: {e}")

    def _task_loan_hourly(self) -> None:
        """计息 + 违约判定 + 催收提醒。"""
        try:
            stats = self.loan_collector.hourly()
            if stats.get("accrued_minor"):
                self.log.debug(f"本小时计息 {stats['accrued_minor']} minor")
            if stats.get("newly_collecting"):
                self.log.warning(f"新增 {stats['newly_collecting']} 笔贷款进入追债")
            self.loan_collector.remind()
        except Exception as e:
            self.log.error(f"贷款维护任务失败: {e}")

    def _task_loan_watch(self) -> None:
        """持续追债扫描。只针对处于追债状态的在线玩家。"""
        try:
            self.loan_collector.watch()
        except Exception as e:
            self.log.error(f"追债扫描失败: {e}")

    def _task_stock_price(self) -> None:
        try:
            updated = self.stock_service.update_prices()
            if updated:
                self.log.debug(f"已更新 {len(updated)} 支股票的价格")
        except Exception as e:
            self.log.error(f"股价更新失败: {e}")

    def _task_ui_refresh(self) -> None:
        try:
            self.ui_service.refresh_all()
        except Exception as e:
            self.log.error(f"显示刷新失败: {e}")

    # ---------- 央行 ----------

    def _task_bank_accrual(self) -> None:
        """存款计息。按整小时单利，时间桶幂等，重跑不双记。"""
        try:
            stats = self.bank_deposit.accrue_all()
            if stats.get("interest_minor"):
                self.log.debug(
                    f"存款计息 {stats['interest_minor']} minor（{stats['count']} 笔）"
                )
        except Exception as e:
            self.log.error(f"存款计息失败: {e}")

    def _task_bank_monitor(self) -> None:
        """金融监管巡检：坏账核销 + 风险告警。"""
        try:
            done = self.bank_regulator.auto_write_off_defaulted()
            if done:
                self.log.warning(f"自动核销 {len(done)} 笔坏账")
            produced = self.bank_regulator.check()
            for alert in produced:
                if not alert.acknowledged:
                    self.log.warning(f"[金融监管] {alert.title}")
                    self._notify_governors(f"§c[央行告警] {alert.title}\n§7{alert.detail or ''}")
        except Exception as e:
            self.log.error(f"金融监管巡检失败: {e}")

    def _task_bank_policy(self) -> None:
        """货币政策自动调节（简化泰勒规则，带死区/步长/冷却）。"""
        try:
            result = self.bank_policy.auto_adjust()
            if result.get("detail"):
                self.log.info(f"[央行] {result['detail']}")
        except Exception as e:
            self.log.error(f"货币政策自动调节失败: {e}")

    def _task_bank_maturity(self) -> None:
        """到期处理：定期转活期、理财结算、信用卡出账、按揭到期检查。"""
        try:
            matured = self.bank_deposit.process_maturity()
            for item in matured:
                player = self._player_by_account(item["holder_account_id"])
                if player is not None:
                    player.send_message(self.text(
                        "bank.time.matured", value=format_amount(item["total_minor"])
                    ))
            for item in self.bank_wealth.settle_matured():
                player = self._player_by_account(item["holder_account_id"])
                if player is not None:
                    player.send_message(self.text(
                        "bank.wealth.settled",
                        value=format_amount(item["paid_minor"]),
                        profit=format_amount(item["profit_minor"]),
                    ))
            self.bank_lending.run_statements()
            self.bank_lending.process_mortgage_due()
        except Exception as e:
            self.log.error(f"央行到期处理失败: {e}")

    def _task_auction_settle(self) -> None:
        """结算所有已到点的拍卖：最高出价者得、流拍归还、物品投递（在线即发）。"""
        try:
            done = self.auction_service.settle_open()
            if done:
                self.log.debug(f"本周期结算 {done} 场拍卖")
        except Exception as e:
            self.log.error(f"拍卖结算任务失败: {e}")

    def _notify_governors(self, text: str) -> None:
        """把告警推给在线行长。离线不影响，告警已落库备查。"""
        try:
            for gov in self.bank_service.list_governors():
                player = self._player_by_account(gov.account_id)
                if player is not None:
                    player.send_message(text)
        except Exception as e:
            self.log.debug(f"通知行长失败: {e}")

    def _player_by_account(self, account_id: str):
        try:
            account = self.economy_service.repo.get_account(account_id)
            if account is None or not account.current_name:
                return None
            return self.server.get_player(account.current_name)
        except Exception:
            return None

    # ==================== 供 UI / 命令使用的辅助 ====================

    def text(self, key: str, **kwargs) -> str:
        if self.i18n is None:
            return key
        return self.i18n.t(key, **kwargs)

    def error_text(self, code: ErrorCode, detail: str = "") -> str:
        """把错误码翻成玩家看得懂的话。有更具体的说明时优先用它。"""
        if detail:
            return f"§c{detail}"
        key = CODE_LANG_KEY.get(code)
        if key is None:
            return f"§c{code.value}"
        return f"§c{self.text(key)}"

    def err_message(self, result) -> str:
        if result is None:
            return self.text("err.internal")
        return self.error_text(result.code, result.message)

    # ---------- 玩家与权限 ----------

    @staticmethod
    def is_player(sender) -> bool:
        return hasattr(sender, "send_form") and hasattr(sender, "xuid")

    @staticmethod
    def is_admin(sender) -> bool:
        return bool(getattr(sender, "is_op", False))

    def can_manage(self, player, shop: Dict[str, Any]) -> bool:
        if self.is_admin(player):
            return True
        account = self.economy_api.api_ensure_player(player)
        if account is None:
            return False
        return account.account_id == shop.get("owner_account_id")

    def owner_display(self, shop: Dict[str, Any]) -> str:
        owner_id = shop.get("owner_account_id")
        if not owner_id:
            return "?"
        account = self.economy_service.repo.get_account(owner_id)
        if account is None:
            return "?"
        return account.current_name or owner_id[:8]

    def is_shop_active(self, shop: Dict[str, Any]) -> bool:
        return shop.get("status") == ShopStatus.ACTIVE.value

    def sponsor_of(self, shop: Dict[str, Any]) -> Optional[str]:
        """这家店背后公司的赞助商署名。用于店铺详情页的广告。"""
        if self.fundraising_service is None:
            return None
        owner = shop.get("owner_account_id")
        if not owner:
            return None
        try:
            return self.fundraising_service.sponsor_line(owner)
        except Exception:
            return None

    def escrow_balance(self, shop: Dict[str, Any]) -> int:
        escrow = shop.get("escrow_account_id")
        if not escrow:
            return 0
        return self.economy_service.get_balance(escrow).available_minor

    # ---------- 建店会话状态 ----------

    def arm_build(self, player, state: Dict[str, Any]) -> None:
        self._building[player.name] = dict(state)

    def is_building(self, player) -> bool:
        return player.name in self._building

    def take_build_state(self, player) -> Optional[Dict[str, Any]]:
        return self._building.pop(player.name, None)

    def cancel_build(self, player) -> bool:
        return self._building.pop(player.name, None) is not None

    # ---------- 名称转换 ----------

    def shop_type_name(self, shop_type: str) -> str:
        return self.text(f"shop.type_name.{shop_type}")

    def tx_kind_name(self, kind: str) -> str:
        return self.text(f"tx.kind.{kind}")

    def direction_name(self, direction: str) -> str:
        return self.text(f"direction.{direction}")

    # ---------- 热重载 ----------

    def reload_all(self) -> None:
        self.cfg.reload()
        self.i18n.reload()
        self.price_table = load_json_resource(
            Path(str(self.data_folder)) / "prices.json", _bundled_price_table(), self.log
        ) or _bundled_price_table()
        self.pricing.reload_table(self.price_table)
        self.pricing.ensure_states()
        self.log.info("配置与价格表已重载")

    # ==================== 对外 API ====================

    def api_get_economy(self) -> EconomyAPI:
        """给其它插件用的经济门面。"""
        return self.economy_api

    def api_analyze_economy(self) -> list:
        """跑一次经济分析，返回建议列表（供运维面板 / 外部调用）。"""
        return self.ai.analyze() if self.ai else []

    def api_economy_report(self) -> str:
        if self.metrics_query is None:
            return ""
        return self.metrics_query.format_report(money_formatter=format_amount)


try:  # Endstone 0.11 事件
    from endstone.event import (
        PlayerJoinEvent,
        PlayerPickupItemEvent,
        PlayerQuitEvent,
        PlayerRespawnEvent,
        event_handler,
    )
except ImportError:  # pragma: no cover
    event_handler = None

    def event_handler(*args, **kwargs):
        def wrapper(fn):
            return fn

        return wrapper


class JoinListener:
    """玩家进服时确保账户存在 —— 避免第一次交易时才开户造成的卡顿。"""

    def __init__(self, plugin: AimarketPlugin):
        self.plugin = plugin

    @event_handler
    def on_join(self, event: PlayerJoinEvent) -> None:
        try:
            account = self.plugin.economy_api.api_ensure_player(event.player)
            # 外部货币桥接：上线对账（待推→推内部值；无→采纳外部值）
            try:
                if account is not None and self.plugin.bridge is not None:
                    self.plugin.economy_service.bridge_reconcile(account.account_id)
            except Exception as e:
                self.plugin.log.debug(f"桥接对账失败: {e}")
            # 行长名单绑定：config 里填了名字，玩家一上线就落成 XUID
            self.plugin.bank_service.sync_governors(event.player)
            # 上线投递：之前拍下 / 流拍归还但当时离线的物品
            try:
                account = self.plugin.economy_api.api_ensure_player(event.player)
                if account is not None:
                    delivered = self.plugin.auction_service.deliver_pending(
                        account.account_id, event.player
                    )
                    if delivered > 0:
                        event.player.send_message(self.plugin.text("auction.delivered", n=delivered))
            except Exception as e:
                self.plugin.log.debug(f"拍卖物品投递失败: {e}")
            # 立刻渲染一次自选显示，不用等下一个刷新周期
            self.plugin.ui_service.on_player_join(event.player)
        except Exception as e:
            self.plugin.log.error(f"玩家进服处理失败: {e}")

    @event_handler
    def on_quit(self, event: PlayerQuitEvent) -> None:
        try:
            self.plugin.ui_service.on_player_quit(event.player)
        except Exception as e:
            self.plugin.log.debug(f"玩家退出清理失败: {e}")


class BusinessListener:
    """贷款与公司的相关事件。

    关于"物品进背包就自动变卖还债"：Endstone 0.11.x **没有**覆盖所有入包途径的事件
    （`InventoryMoveItemEvent` / `InventoryClickEvent` 是 Java Bukkit 的概念，
    Endstone 从未提供；容器类事件还在 Unreleased 未发布）。唯一可用的是
    `PlayerPickupItemEvent`，而合成、开箱、/give、交易、漏斗都漏网。

    所以这里的定位是**加速**，不是唯一手段：真正兜底的是 loan collector 的定时扫描。
    两者叠加，让抵债在关键路径上近乎实时。
    """

    def __init__(self, plugin: AimarketPlugin):
        self.plugin = plugin

    @event_handler
    def on_pickup(self, event: PlayerPickupItemEvent) -> None:
        self._nudge(event)

    @event_handler
    def on_respawn(self, event: PlayerRespawnEvent) -> None:
        self._nudge(event)

    def _nudge(self, event) -> None:
        try:
            player = getattr(event, "player", None)
            if player is None:
                return
            self.plugin.loan_collector.on_player_event(player)
        except Exception as e:
            self.plugin.log.debug(f"追债事件加速失败: {e}")


def _bundled_price_table() -> Dict[str, Any]:
    """随包分发的默认价格表。"""
    path = Path(__file__).resolve().parent / "resources" / "prices.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"categories": [], "items": {}}
