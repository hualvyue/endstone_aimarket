"""内置显示项。

每个 provider 只依赖 DisplayContext，不直接访问插件内部 ——
以后要加新指标（比如"本周纳税额"），在这里加一条 register 就行，界面不用动。
"""
from __future__ import annotations

from typing import Optional

from ..core.money import format_amount
from .registry import DisplayRegistry


def register_builtin(registry: DisplayRegistry) -> None:
    # ---------- 个人资产 ----------

    registry.register(
        "balance",
        lambda ctx: f"§7余额 §a{format_amount(ctx.balance_minor())}",
        name="我的余额",
        description="当前账户余额",
        default_on=True,
        sort_order=10,
    )

    registry.register(
        "debt",
        _debt,
        name="我的欠款",
        description="未还清的贷款本息合计",
        sort_order=20,
    )

    registry.register(
        "debt_due",
        _debt_due,
        name="还款期限",
        description="最近一笔贷款的到期时间",
        sort_order=30,
    )

    registry.register(
        "portfolio",
        _portfolio,
        name="持仓股票市值",
        description="你持有的全部股票市值与盈亏",
        sort_order=40,
    )

    registry.register(
        "shop_count",
        _shop_count,
        name="我的商店数",
        description="你名下的商店数量",
        sort_order=50,
    )

    # ---------- 公司 ----------

    registry.register(
        "company_nav",
        _company_nav,
        name="我的公司净资产",
        description="你创办的公司的真实资产扣除负债",
        sort_order=60,
    )

    registry.register(
        "company_count",
        _company_count,
        name="我的公司数",
        sort_order=65,
    )

    # ---------- 市场 ----------

    registry.register(
        "market_cap",
        _market_cap,
        name="股市总市值",
        description="全服上市公司市值合计",
        sort_order=70,
    )

    registry.register(
        "shop_total",
        _shop_total,
        name="全服商店数",
        sort_order=80,
    )

    # ---------- 动态：指定某支股票 ----------

    registry.register_prefix(
        "stock",
        _stock_price,
        name="股票行情",
        description="指定一支股票，显示它的价格与涨跌",
        sort_order=500,
    )

    # ---------- 动态：自己名下的某个商店 ----------

    registry.register_prefix(
        "shop",
        _shop_revenue,
        name="指定商店",
        description="指定一家你的商店，显示它的营收与库存",
        sort_order=510,
    )

    # ---------- 央行 ----------

    registry.register(
        "bank_deposit",
        _bank_deposit,
        name="我的银行存款",
        description="活期 + 定期 + 理财的资金合计",
        sort_order=90,
    )

    registry.register(
        "bank_credit",
        _bank_credit,
        name="我的信用欠款",
        description="透支与信用卡的未偿合计",
        sort_order=92,
    )

    registry.register(
        "bank_rate",
        _bank_rate,
        name="央行基准利率",
        description="当前基准利率与准备金率",
        sort_order=94,
    )


# ==================== provider 实现 ====================

def _debt(ctx) -> Optional[str]:
    service = getattr(ctx.plugin, "loan_service", None)
    if service is None:
        return None
    total = service.total_debt_of(ctx.account_id)
    if total <= 0:
        return None
    return f"§7欠款 §c{format_amount(total)}"


def _debt_due(ctx) -> Optional[str]:
    import time

    service = getattr(ctx.plugin, "loan_service", None)
    if service is None:
        return None
    loans = service.loans_of(ctx.account_id)
    if not loans:
        return None
    soonest = min(loans, key=lambda l: l.due_at_ms)
    try:
        when = time.strftime("%m-%d %H:%M", time.localtime(soonest.due_at_ms / 1000))
    except Exception:
        when = "-"
    flag = "§c已逾期" if soonest.due_at_ms < time.time() * 1000 else "§7到期"
    return f"{flag} §f{when}"


def _portfolio(ctx) -> Optional[str]:
    service = getattr(ctx.plugin, "stock_service", None)
    if service is None:
        return None
    summary = service.portfolio_summary(ctx.account_id)
    if not summary["count"]:
        return None
    value = summary["market_value_minor"]
    profit = summary["profit_minor"]
    # 中国市场习惯：涨红跌绿
    color = "§c" if profit > 0 else ("§a" if profit < 0 else "§7")
    sign = "+" if profit > 0 else ""
    return f"§7持仓 §f{format_amount(value)} {color}{sign}{format_amount(profit)}"


def _shop_count(ctx) -> Optional[str]:
    service = getattr(ctx.plugin, "shop_repo", None)
    if service is None:
        return None
    count = service.count_by_owner(ctx.account_id)
    if count <= 0:
        return None
    return f"§7商店 §f{count} 家"


def _company_nav(ctx) -> Optional[str]:
    companies = getattr(ctx.plugin, "company_service", None)
    if companies is None:
        return None
    owned = [
        c for c in companies.companies_of(ctx.account_id)
        if c.founder_account_id == ctx.account_id and c.is_active
    ]
    if not owned:
        return None
    total = sum(companies.nav_minor(c) for c in owned)
    return f"§7公司净资产 §a{format_amount(total)}"


def _company_count(ctx) -> Optional[str]:
    companies = getattr(ctx.plugin, "company_service", None)
    if companies is None:
        return None
    owned = [c for c in companies.companies_of(ctx.account_id) if c.is_active]
    if not owned:
        return None
    return f"§7公司 §f{len(owned)} 家"


def _market_cap(ctx) -> Optional[str]:
    service = getattr(ctx.plugin, "stock_service", None)
    if service is None:
        return None
    cap = service.total_market_cap()
    if cap <= 0:
        return None
    return f"§7股市总额 §e{format_amount(cap)}"


def _shop_total(ctx) -> Optional[str]:
    repo = getattr(ctx.plugin, "shop_repo", None)
    if repo is None:
        return None
    stats = repo.stats()
    if not stats.get("active"):
        return None
    return f"§7全服商店 §f{stats['active']} 家"


def _stock_price(ctx, name: str) -> Optional[str]:
    """`stock:公司名` —— 显示指定股票的现价，并用涨红跌绿表达与每股净资产的偏离。"""
    service = getattr(ctx.plugin, "stock_service", None)
    if service is None:
        return None
    listing = None
    label = name
    if service.companies is not None:
        company = service.companies.repo.get_by_name(name)
        if company is not None:
            listing = service.listing_of(company.account_id)
            label = company.name
    if listing is None:
        return None
    price = listing.last_price_minor
    dev = price - listing.nav_per_share_minor
    color = "§c" if dev > 0 else ("§a" if dev < 0 else "§7")
    trend = f"{color}{dev * 10000 // max(1, listing.nav_per_share_minor) / 100:+.2f}%"
    return f"§7{label} §f{format_amount(price, with_symbol=False)} {trend}"


def _shop_revenue(ctx, name: str) -> Optional[str]:
    service = getattr(ctx.plugin, "shop_service", None)
    if service is None:
        return None
    for shop in service.my_shops(ctx.account_id):
        import json

        try:
            info = json.loads(shop.get("offer_item_json") or "{}")
        except Exception:
            info = {}
        if info.get("name") == name:
            stock = int(shop.get("stock_qty", 0) or 0)
            return f"§7{name} §f库存 {stock}"
    return None


# ==================== 央行 ====================

def _bank_deposit(ctx) -> Optional[str]:
    service = ctx.bank_deposit
    if service is None:
        return None
    total = service.total_of(ctx.account_id)
    if total <= 0:
        return None
    return f"§7银行存款 §b{format_amount(total)}"


def _bank_credit(ctx) -> Optional[str]:
    lending = ctx.bank_lending
    if lending is None:
        return None
    overdraft = lending.overdraft_used(ctx.account_id)
    card = lending.card_used(ctx.account_id)
    total = overdraft + card
    if total <= 0:
        return None
    return f"§7信用欠款 §c{format_amount(total)}"


def _bank_rate(ctx) -> Optional[str]:
    bank = ctx.bank
    if bank is None:
        return None
    try:
        policy = bank.policy()
    except Exception:
        return None
    if not bank.enabled():
        return None
    return (
        f"§7基准利率 §e{policy.base_rate_bps_per_day / 100:.2f}%§7"
        f" ｜ 准备金 §f{policy.rrr_bps / 100:.1f}%"
    )
