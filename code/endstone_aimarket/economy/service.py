"""经济内核：账户、记账、冻结与结算。

三条铁律：
1. 任何一次资金变动都必须落在**一个数据库事务**里，且分录必须配平；
2. 业务失败通过 Result 返回，**绝不抛异常中断调用方的流程**；
3. 幂等键重复时返回上一次的结果，绝不重复扣款。
"""
from __future__ import annotations

import time
from typing import Iterable, List, Optional

from ..core.constants import AccountKind, AccountStatus, TxKind, TxStatus
from ..core.database import Database
from ..core.errors import AimarketError, ErrorCode
from ..core.ids import idempotency_key as make_idem, new_uuid7
from ..core.money import MAX_MINOR, add_bps, apply_bps, split_tax
from ..core.result import Result
from .models import Account, Balance, Entry, Receipt, TxRecord, ROLE_PAYEE, ROLE_PAYER, ROLE_TAX
from .repository import (
    SYSTEM_MINT_ACCOUNT_ID,
    TREASURY_ACCOUNT_ID,
    EconomyRepository,
    now_ms,
)


class EconomyService:
    def __init__(self, db: Database, log=None, *, initial_balance_minor: int = 0):
        self.db = db
        self.log = log
        self.repo = EconomyRepository(db)
        self.initial_balance_minor = max(0, int(initial_balance_minor))
        # 外部货币桥接（umoney / arc_core / EconomyAPI…）。None = 纯内部模式。
        self.bridge = None

    def attach_bridge(self, bridge) -> None:
        """挂载外部货币桥接。必须在 bootstrap 之后调用。"""
        self.bridge = bridge

    def bootstrap(self) -> None:
        """建系统账户。必须在迁移之后调用。"""
        with self.db.transaction():
            self.repo.ensure_system_accounts()

    # ==================== 账户解析 ====================

    def ensure_player(
        self,
        *,
        xuid: Optional[str] = None,
        player_uuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Result[Account]:
        """登录/首次交易时调用：找到或创建玩家账户，并同步名字。

        优先级：xuid > uuid > name。只要有 xuid 就认 xuid，改名不影响账户。
        """
        with self.db.transaction():
            account = None
            if xuid:
                account = self.repo.get_by_xuid(xuid)
            if account is None and player_uuid:
                account = self.repo.get_by_uuid(player_uuid)
            if account is None and name:
                account = self.repo.get_by_name(name)

            if account is None:
                # 全新玩家：拒绝为「只有名字没有 xuid」的调用开户，避免把重名玩家
                # 错绑到同一个账户上（账户标识必须以唯一 ID 为准，不能用名字）
                if not xuid and not player_uuid:
                    return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
                account = self.repo.create_account(
                    new_uuid7(),
                    AccountKind.PLAYER.value,
                    xuid=xuid,
                    player_uuid=player_uuid,
                    name=name,
                )
                # 桥接模式：老玩家在外部经济（umoney 等）里的存量直接继承，
                # 没有外部记录才发 initial_balance 赠款
                initial = self.initial_balance_minor
                imported = False
                if self.bridge is not None and self.bridge.active and name:
                    external_minor = self.bridge.get_minor(name)
                    if external_minor is not None and external_minor > 0:
                        initial = external_minor
                        imported = True
                if initial > 0:
                    self._mint_locked(
                        account.account_id,
                        initial,
                        reason="bridge_import" if imported else "initial_balance",
                        idem=make_idem("initial", account.account_id),
                    )
                    account = self.repo.get_account(account.account_id)
                if self.log:
                    if imported:
                        self.log.info(
                            f"新建账户 {account.account_id} ({name}) 从外部经济导入余额 "
                            f"{initial} minor"
                        )
                    else:
                        self.log.info(f"新建账户 {account.account_id} ({name}) 初始余额 "
                                      f"{self.initial_balance_minor} minor")
                return Result.success(account)

            # 已有账户：补全漂移的字段并登记改名
            changed = False
            if xuid and not account.xuid:
                self.db.update("accounts", {"xuid": str(xuid)}, "account_id = ?", (account.account_id,))
                changed = True
            if player_uuid and not account.player_uuid:
                self.db.update(
                    "accounts", {"player_uuid": str(player_uuid)}, "account_id = ?",
                    (account.account_id,),
                )
                changed = True
            if name and account.current_name != name:
                self.repo.set_current_name(account.account_id, name)
                changed = True
            if changed:
                account = self.repo.get_account(account.account_id)
            return Result.success(account)

    def resolve(
        self,
        *,
        account_id: Optional[str] = None,
        xuid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Result[Account]:
        """只读解析账户，不创建。"""
        account = None
        if account_id:
            account = self.repo.get_account(account_id)
        elif xuid:
            account = self.repo.get_by_xuid(xuid)
        elif name:
            account = self.repo.get_by_name(name)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        return Result.success(account)

    # ==================== 查询 ====================

    def get_balance(self, account_id: str) -> Balance:
        total, held = self.repo.read_balance(account_id)
        return Balance(account_id=account_id, balance_minor=total, held_minor=held)

    def balance_of(self, *, account_id: str = None, xuid: str = None, name: str = None) -> Result[Balance]:
        resolved = self.resolve(account_id=account_id, xuid=xuid, name=name)
        if not resolved.ok:
            return Result.failure(resolved.code)
        account = resolved.value
        return Result.success(
            Balance(account.account_id, account.balance_minor, account.held_minor)
        )

    def history(self, account_id: str, *, limit: int = 20, offset: int = 0) -> List[TxRecord]:
        return self.repo.history(account_id, limit=limit, offset=offset)

    def total_supply(self) -> int:
        """M0 口径的货币供应量：玩家 + 公司持有的货币之和。

        公司账户里沉淀的现金同样是流通中的货币，漏算会让恒等式与通胀指标失真。
        """
        return self.repo.total_agent_balance()

    def player_supply(self) -> int:
        """只看玩家手里的货币，用于人均/分布类指标。"""
        return self.repo.total_player_balance()

    def minted_total(self) -> int:
        return self.repo.minted_total()

    def treasury_balance(self) -> int:
        return self.repo.read_balance(TREASURY_ACCOUNT_ID)[0]

    # ==================== 铸造 / 销毁 ====================

    def deposit(
        self,
        account_id: str,
        amount_minor: int,
        *,
        reason: str = "deposit",
        idem: Optional[str] = None,
        actor_account_id: Optional[str] = None,
    ) -> Result[Receipt]:
        """给账户加钱（从铸币账户划出，会计上等于增发）。"""
        if amount_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        return self._run_locked(
            lambda: self._mint_locked(
                account_id, amount_minor, reason=reason, idem=idem, actor=actor_account_id
            ),
            idem or make_idem("deposit", account_id, amount_minor, reason),
        )

    def withdraw(
        self,
        account_id: str,
        amount_minor: int,
        *,
        reason: str = "withdraw",
        idem: Optional[str] = None,
        actor_account_id: Optional[str] = None,
    ) -> Result[Receipt]:
        """从账户扣钱并销毁（回收货币）。"""
        if amount_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        return self._run_locked(
            lambda: self._burn_locked(
                account_id, amount_minor, reason=reason, idem=idem, actor=actor_account_id
            ),
            idem or make_idem("withdraw", account_id, amount_minor, reason),
        )

    # ==================== 转账 ====================

    def transfer(
        self,
        src_account_id: str,
        dst_account_id: str,
        amount_minor: int,
        *,
        reason: str = "transfer",
        idem: Optional[str] = None,
        actor_account_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        kind: Optional[str] = None,
    ) -> Result[Receipt]:
        if amount_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if src_account_id == dst_account_id:
            return Result.failure(ErrorCode.INVALID_AMOUNT, "不能给自己转账")
        return self._run_locked(
            lambda: self._post(
                kind=kind or TxKind.TRANSFER.value,
                entries=[
                    Entry(src_account_id, ROLE_PAYER, -amount_minor),
                    Entry(dst_account_id, ROLE_PAYEE, amount_minor),
                ],
                idem=idem or make_idem("transfer", src_account_id, dst_account_id, amount_minor),
                actor=actor_account_id or src_account_id,
                correlation_id=reason,
                metadata=metadata,
                gross=amount_minor,
                net=amount_minor,
            ),
            idem or make_idem("transfer", src_account_id, dst_account_id, amount_minor),
        )

    def transfer_taxed(
        self,
        src_account_id: str,
        dst_account_id: str,
        amount_minor: int,
        *,
        tax_bps: int = 0,
        reason: str = "transfer_taxed",
        idem: Optional[str] = None,
        actor_account_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Result[Receipt]:
        """带税转账：付款方出全额，收款方得净额，差额进国库。三方分录自动配平。"""
        if amount_minor <= 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        net, tax = split_tax(amount_minor, tax_bps)
        entries = [
            Entry(src_account_id, ROLE_PAYER, -amount_minor),
            Entry(dst_account_id, ROLE_PAYEE, net),
        ]
        if tax > 0:
            entries.append(Entry(TREASURY_ACCOUNT_ID, ROLE_TAX, tax))
        return self._run_locked(
            lambda: self._post(
                kind=TxKind.TAX.value if tax > 0 else TxKind.TRANSFER.value,
                entries=entries,
                idem=idem or make_idem("taxed", src_account_id, dst_account_id, amount_minor, tax_bps),
                actor=actor_account_id or src_account_id,
                correlation_id=reason,
                metadata=metadata,
                gross=amount_minor,
                net=net,
                tax=tax,
            ),
            idem or make_idem("taxed", src_account_id, dst_account_id, amount_minor, tax_bps),
        )

    # ==================== 冻结（TCC 的 Try/Cancel）====================

    def freeze(
        self, account_id: str, amount_minor: int, *, tx_id: Optional[str] = None
    ) -> Result[int]:
        """预留资金：只加 held，不动 balance。返回可动用余额。"""
        if amount_minor < 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if amount_minor == 0:
            return Result.success(self.get_balance(account_id).available_minor)
        with self.db.transaction():
            account = self.repo.get_account(account_id)
            if account is None:
                return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
            if account.status != AccountStatus.ACTIVE.value:
                return Result.failure(ErrorCode.ACCOUNT_FROZEN)
            if account.available_minor < amount_minor:
                return Result.failure(ErrorCode.INSUFFICIENT_FUNDS)
            self.repo.apply_balance_delta(account_id, held_delta=amount_minor)
        if self.log:
            self.log.debug(f"冻结 {account_id} {amount_minor} minor (tx={tx_id})")
        return Result.success(self.get_balance(account_id).available_minor)

    def release_freeze(self, account_id: str, amount_minor: int, *, tx_id: Optional[str] = None) -> Result[None]:
        """释放冻结（Cancel 路径）。不产生账本分录，因为钱从未真正动过。"""
        if amount_minor <= 0:
            return Result.success(None)
        with self.db.transaction():
            total, held = self.repo.read_balance(account_id)
            real = min(int(amount_minor), held)
            if real > 0:
                self.repo.apply_balance_delta(account_id, held_delta=-real)
        return Result.success(None)

    def freeze_stock(self, shop_id: str, quantity: int) -> Result[None]:
        """冻结商店库存（出售店的 Try 阶段）。"""
        if quantity <= 0:
            return Result.success(None)
        with self.db.transaction():
            row = self.db.query_one("SELECT * FROM shops WHERE shop_id = ?", (shop_id,))
            if row is None:
                return Result.failure(ErrorCode.SHOP_NOT_FOUND)
            if int(row["is_infinite"]) == 0:
                available = int(row["stock_qty"]) - int(row["held_stock_qty"])
                if available < quantity:
                    return Result.failure(ErrorCode.SHOP_SOLD_OUT)
                self.db.update(
                    "shops",
                    {"held_stock_qty": int(row["held_stock_qty"]) + int(quantity), "updated_at_ms": now_ms()},
                    "shop_id = ?",
                    (shop_id,),
                )
        return Result.success(None)

    def release_stock(self, shop_id: str, quantity: int) -> Result[None]:
        if quantity <= 0:
            return Result.success(None)
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT held_stock_qty FROM shops WHERE shop_id = ?", (shop_id,)
            )
            if row is None:
                return Result.failure(ErrorCode.SHOP_NOT_FOUND)
            real = min(int(quantity), int(row["held_stock_qty"]))
            if real > 0:
                self.db.update(
                    "shops",
                    {
                        "held_stock_qty": int(row["held_stock_qty"]) - real,
                        "updated_at_ms": now_ms(),
                    },
                    "shop_id = ?",
                    (shop_id,),
                )
        return Result.success(None)

    def confirm_stock(self, shop_id: str, quantity: int) -> Result[None]:
        """确认消耗冻结库存：held 与 stock 同时减少。"""
        if quantity <= 0:
            return Result.success(None)
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT stock_qty, held_stock_qty, is_infinite FROM shops WHERE shop_id = ?", (shop_id,)
            )
            if row is None:
                return Result.failure(ErrorCode.SHOP_NOT_FOUND)
            if int(row["is_infinite"]) != 0:
                return Result.success(None)
            held = int(row["held_stock_qty"])
            real = min(int(quantity), held)
            update = {"updated_at_ms": now_ms()}
            if real > 0:
                update["held_stock_qty"] = held - real
                update["stock_qty"] = max(0, int(row["stock_qty"]) - real)
            self.db.update("shops", update, "shop_id = ?", (shop_id,))
        return Result.success(None)

    def add_stock(self, shop_id: str, quantity: int) -> Result[None]:
        with self.db.transaction():
            self.db.execute(
                "UPDATE shops SET stock_qty = stock_qty + ?, updated_at_ms = ? WHERE shop_id = ?",
                (int(quantity), now_ms(), shop_id),
            )
        return Result.success(None)

    # ==================== 内部：事务内记账 ====================

    def _run_locked(self, fn, idem: str) -> Result[Receipt]:
        """统一入口：幂等检查 + 单事务执行 + 配平校验。"""
        try:
            existing = self.repo.find_tx_by_idempotency(idem)
            if existing is not None:
                return Result.success(
                    Receipt(
                        tx_id=existing["tx_id"],
                        kind=existing["kind"],
                        status=existing["status"],
                        created_at_ms=int(existing["created_at_ms"]),
                        idempotent_replay=True,
                    )
                )
            with self.db.transaction():
                result = fn()
                if not result.ok:
                    # 主动回滚：抛出被 transaction 捕获后回滚，再转成 Result
                    raise _Rollback(result)
            return result
        except _Rollback as rb:
            return rb.result
        except AimarketError as e:
            if self.log:
                self.log.error(f"经济操作失败 {idem}: {e}")
            return Result.failure(e.code, e.message)
        except Exception as e:
            if self.log:
                import traceback
                self.log.error(f"经济操作异常 {idem}: {e}\n{traceback.format_exc()}")
            return Result.failure(ErrorCode.INTERNAL, str(e))

    def post_in_transaction(
        self,
        *,
        kind: str,
        entries: List[Entry],
        idem: str,
        tx_id: Optional[str] = None,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        gross: int = 0,
        net: int = 0,
        tax: int = 0,
    ) -> Result[Receipt]:
        """在**调用方已开启的事务**内写一笔账。

        交易协调器专用：库存扣减、资金划转、价格更新必须落在同一个事务里，
        所以这里不能再自己开事务，只能借道调用方的事务。
        幂等由 `idem` 承担：重复的 trade 会命中已有记录，直接返回。
        """
        try:
            if tx_id is not None:
                existing = self.db.query_one(
                    "SELECT tx_id FROM ledger_tx WHERE tx_id = ?", (tx_id,)
                )
                if existing is not None:
                    return Result.success(Receipt(tx_id=tx_id, kind=kind, idempotent_replay=True))
            else:
                existing = self.repo.find_tx_by_idempotency(idem)
                if existing is not None:
                    return Result.success(
                        Receipt(
                            tx_id=existing["tx_id"],
                            kind=existing["kind"],
                            status=existing["status"],
                            idempotent_replay=True,
                        )
                    )
            return self._post(
                kind=kind,
                entries=entries,
                idem=idem,
                actor=actor,
                correlation_id=correlation_id,
                metadata=metadata,
                gross=gross,
                net=net,
                tax=tax,
                tx_id=tx_id,
            )
        except AimarketError as e:
            return Result.failure(e.code, e.message)

    def _post(
        self,
        *,
        kind: str,
        entries: List[Entry],
        idem: str,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        gross: int = 0,
        net: int = 0,
        tax: int = 0,
        tx_id: Optional[str] = None,
        supply_delta_minor: int = 0,
        status: str = TxStatus.FILLED.value,
        balance_deltas: Optional[dict] = None,
    ) -> Result[Receipt]:
        """在**当前事务内**写一笔账。调用方必须已开启事务。"""
        merged = self._merge_entries(entries)

        total = sum(e.delta_minor for e in merged)
        if total != 0:
            raise AimarketError(
                ErrorCode.ENTRY_UNBALANCED, f"分录不配平，差额 {total} minor"
            )

        # 负余额预检：system 账户允许透支（它代表货币发行方）
        for entry in merged:
            if entry.delta_minor >= 0:
                continue
            account = self.repo.get_account(entry.account_id)
            if account is None:
                return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
            if account.status == AccountStatus.CLOSED.value:
                return Result.failure(ErrorCode.ACCOUNT_CLOSED)
            if account.kind != AccountKind.SYSTEM.value:
                if account.balance_minor + entry.delta_minor < 0:
                    return Result.failure(
                        ErrorCode.INSUFFICIENT_FUNDS,
                        f"{entry.account_id} 余额不足",
                    )
                available = account.available_minor
                if entry.delta_minor < 0 and available + entry.delta_minor < 0:
                    return Result.failure(
                        ErrorCode.INSUFFICIENT_FUNDS, f"{entry.account_id} 可用余额不足"
                    )

        tx = tx_id or new_uuid7()
        self.repo.create_tx(
            tx,
            idem,
            kind,
            status=status,
            supply_delta_minor=supply_delta_minor,
            correlation_id=correlation_id,
            actor_account_id=actor,
            metadata=metadata,
        )

        applied: List[Entry] = []
        for entry in merged:
            extra_balance = 0
            extra_held = 0
            if balance_deltas and entry.account_id in balance_deltas:
                extra_balance, extra_held = balance_deltas[entry.account_id]
            balance_after = self.repo.apply_balance_delta(
                entry.account_id,
                balance_delta=entry.delta_minor + extra_balance,
                held_delta=extra_held,
            )
            entry.balance_after_minor = balance_after
            self.repo.add_entry(
                tx, entry.account_id, entry.role, entry.delta_minor, balance_after
            )
            applied.append(entry)

        # 外部货币桥接：与账本同一事务记录「提交后应推送的外部余额」，保证不丢
        if self.bridge is not None and self.bridge.active:
            kinds: dict = {}
            for entry in applied:
                if entry.account_id not in kinds:
                    acct = self.repo.get_account(entry.account_id)
                    kinds[entry.account_id] = acct.kind if acct is not None else None
            for entry in applied:
                if kinds.get(entry.account_id) == AccountKind.PLAYER.value:
                    self.db.execute(
                        "INSERT INTO bridge_pending (account_id, pending_minor, updated_at_ms) "
                        "VALUES (?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
                        "pending_minor = excluded.pending_minor, updated_at_ms = excluded.updated_at_ms",
                        (entry.account_id, int(entry.balance_after_minor or 0), now_ms()),
                    )

        # 事务内自校验：这是最后一道防线
        written = self.repo.entry_sum(tx)
        if written != 0:
            raise AimarketError(ErrorCode.ENTRY_UNBALANCED, f"落账后校验失败，差额 {written}")

        return Result.success(
            Receipt(
                tx_id=tx,
                kind=kind,
                status=status,
                gross_minor=gross,
                tax_minor=tax,
                net_minor=net,
                entries=applied,
                created_at_ms=now_ms(),
            )
        )

    @staticmethod
    def _merge_entries(entries: Iterable[Entry]) -> List[Entry]:
        """把同一账户同一角色的分录合并，避免 UNIQUE(tx_id, account_id, role) 冲突。"""
        bucket: dict = {}
        order: List[tuple] = []
        for entry in entries:
            if entry.delta_minor == 0:
                continue
            key = (entry.account_id, entry.role)
            if key not in bucket:
                bucket[key] = 0
                order.append(key)
            bucket[key] += entry.delta_minor
        return [Entry(account_id=k[0], role=k[1], delta_minor=bucket[k]) for k in order]

    # ---------- 铸币 / 销毁的内部实现（假定已在事务中） ----------

    def _mint_locked(
        self,
        account_id: str,
        amount_minor: int,
        *,
        reason: str,
        idem: Optional[str],
        actor: Optional[str] = None,
    ) -> Result[Receipt]:
        account = self.repo.get_account(account_id)
        if account is None:
            return Result.failure(ErrorCode.ACCOUNT_NOT_FOUND)
        if account.kind != AccountKind.SYSTEM.value and account.balance_minor + amount_minor > MAX_MINOR:
            return Result.failure(ErrorCode.SUPPLY_LIMIT)
        return self._post(
            kind=TxKind.MINT.value,
            entries=[
                Entry(SYSTEM_MINT_ACCOUNT_ID, "source", -amount_minor),
                Entry(account_id, ROLE_PAYEE, amount_minor),
            ],
            idem=idem or make_idem("mint", account_id, amount_minor, reason),
            actor=actor,
            correlation_id=reason,
            gross=amount_minor,
            net=amount_minor,
            supply_delta_minor=amount_minor,
        )

    def _burn_locked(
        self,
        account_id: str,
        amount_minor: int,
        *,
        reason: str,
        idem: Optional[str],
        actor: Optional[str] = None,
    ) -> Result[Receipt]:
        return self._post(
            kind=TxKind.BURN.value,
            entries=[
                Entry(account_id, ROLE_PAYER, -amount_minor),
                Entry(SYSTEM_MINT_ACCOUNT_ID, "sink", amount_minor),
            ],
            idem=idem or make_idem("burn", account_id, amount_minor, reason),
            actor=actor,
            correlation_id=reason,
            gross=amount_minor,
            net=amount_minor,
            supply_delta_minor=-amount_minor,
        )

    # ==================== 批量 ====================

    def execute_batch(self, operations, *, idem: str) -> Result[List[Receipt]]:
        """把多个写操作打包成一个事务：要么全部生效，要么全部不生效。

        operations: 一个可调用序列，每个元素签名为 fn() -> Result[Receipt]。
        典型用法：把玩家余额转到 escrow 的同时扣掉库存。
        """
        try:
            existing = self.repo.find_tx_by_idempotency(idem)
            if existing is not None:
                return Result.success([])
            receipts: List[Receipt] = []
            with self.db.transaction():
                for op in operations:
                    result = op()
                    if not result.ok:
                        raise _Rollback(result)
                    receipts.append(result.value)
            return Result.success(receipts)
        except _Rollback as rb:
            return Result.failure(rb.result.code, rb.result.message)
        except AimarketError as e:
            return Result.failure(e.code, e.message)
        except Exception as e:
            if self.log:
                self.log.error(f"批量操作异常 {idem}: {e}")
            return Result.failure(ErrorCode.INTERNAL, str(e))

    # ==================== 外部货币桥接 ====================

    def push_pending(self, limit: int = 100) -> int:
        """把待推送的余额同步到外部钱包（定时任务调用）。返回推送条数。"""
        if self.bridge is None or not self.bridge.active:
            return 0
        rows = self.db.query_all(
            "SELECT account_id, pending_minor FROM bridge_pending "
            "ORDER BY updated_at_ms LIMIT ?", (int(limit),)
        )
        pushed = 0
        for row in rows:
            account = self.repo.get_account(row["account_id"])
            if account is None:
                self.db.execute("DELETE FROM bridge_pending WHERE account_id = ?", (row["account_id"],))
                continue
            if self.bridge.set_minor(account.current_name or "", int(row["pending_minor"])):
                self.db.execute(
                    "DELETE FROM bridge_pending WHERE account_id = ?", (row["account_id"],)
                )
                pushed += 1
        return pushed

    def bridge_reconcile(self, account_id: str) -> str:
        """玩家上线对账：待推记录 → 推内部值；否则采纳外部值。

        返回 'pushed' / 'adopted' / 'in_sync' / 'skipped'。
        """
        if self.bridge is None or not self.bridge.active:
            return "skipped"
        account = self.repo.get_account(account_id)
        if account is None or account.kind != AccountKind.PLAYER.value:
            return "skipped"
        name = account.current_name or ""
        # 1) 有待推记录：内部赢（防止崩溃窗口里把刚扣的钱还给玩家）
        pending = self.db.query_one(
            "SELECT pending_minor FROM bridge_pending WHERE account_id = ?", (account_id,)
        )
        if pending is not None:
            if self.bridge.set_minor(name, int(pending["pending_minor"])):
                self.db.execute("DELETE FROM bridge_pending WHERE account_id = ?", (account_id,))
            return "pushed"
        # 2) 无待推记录：外部赢（采纳外部插件的 /pay 等改动）
        external_minor = self.bridge.get_minor(name)
        if external_minor is None:
            return "skipped"
        total, held = self.repo.read_balance(account_id)
        diff = int(external_minor) - int(total)
        if diff == 0:
            return "in_sync"
        if diff > 0:
            result = self._mint_locked(
                account_id, diff, reason="bridge_reconcile", idem=make_idem("bridge_rec", account_id, diff)
            )
        else:
            result = self._burn_locked(
                account_id, -diff, reason="bridge_reconcile", idem=make_idem("bridge_rec", account_id, diff)
            )
        if result.ok and self.log:
            self.log.info(
                f"桥接对账 {name}: 采纳外部余额，调整 {diff:+d} minor"
            )
        return "adopted" if result.ok else "skipped"

    # ==================== 管理 ====================

    def admin_adjust(
        self,
        account_id: str,
        delta_minor: int,
        *,
        reason: str = "admin",
        idem: Optional[str] = None,
    ) -> Result[Receipt]:
        if delta_minor == 0:
            return Result.failure(ErrorCode.INVALID_AMOUNT)
        if delta_minor > 0:
            return self.deposit(account_id, delta_minor, reason=reason, idem=idem)
        return self.withdraw(account_id, -delta_minor, reason=reason, idem=idem)

    def audit(self, *, limit: int = 50) -> dict:
        """一致性自检：不平分录 + 余额漂移。健康时应为空。"""
        return {
            "unbalanced": self.repo.audit_unbalanced(limit=limit),
            "drift": self.repo.audit_balance_drift(limit=limit),
            "generated_at_ms": int(time.time() * 1000),
        }

    # ---------- 便捷 ----------

    @staticmethod
    def suggest_tax(gross_minor: int, tax_bps: int) -> tuple:
        """(净额, 税额)，供 UI 在做交易确认时预览。"""
        if tax_bps <= 0:
            return gross_minor, 0
        net, tax = split_tax(gross_minor, tax_bps)
        return net, tax

    @staticmethod
    def price_with_bps(base_minor: int, bps: int) -> int:
        return add_bps(base_minor, bps)

    @staticmethod
    def ratio(base_minor: int, bps: int) -> int:
        return apply_bps(base_minor, bps)


class _Rollback(Exception):
    """内部信号：把失败结果带出事务上下文并触发回滚。"""

    def __init__(self, result: Result):
        super().__init__("rollback")
        self.result = result
