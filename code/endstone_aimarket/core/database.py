"""SQLite 存储层。

设计要点：
1. **有真正的事务**：资金变动涉及「扣款 + 扣库存 + 写流水」多个步骤，
   必须能在同一个原子操作里完成。`transaction()` 上下文默认
   `BEGIN IMMEDIATE`，嵌套时用 SAVEPOINT。
2. **单连接 + 写锁**：WAL 模式下仍可能有并发写冲突，所有写操作串行化，
   配合 `busy_timeout` 与重试，杜绝 `database is locked` 打断交易。
3. **版本化迁移**：`schema_migrations` 记录版本号与校验和，升级幂等且可回滚排查。
4. **外键强制开启**，防止产生孤儿流水。

线程模型：Endstone 的调度与事件回调主要跑在服务主线程，但命令也可能来自
控制台线程。因此连接以 `check_same_thread=False` 打开，由本类的锁来保证安全。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .errors import ErrorCode
from .errors import AimarketError


def split_sql_statements(script: str) -> List[str]:
    """按 `;` 切分 SQL 脚本，丢弃注释与空行。

    只处理本项目自己的迁移脚本（无触发器、无字符串内含分号的情形），
    换取「整个迁移可在一个事务里回滚」的能力。
    """
    statements: List[str] = []
    buffer: List[str] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(buffer).rstrip().rstrip(";"))
            buffer = []
    if buffer:
        tail = "\n".join(buffer).strip()
        if tail:
            statements.append(tail.rstrip(";"))
    return [s for s in statements if s.strip()]


class Database:
    def __init__(self, db_path, log=None, *, busy_timeout_ms: int = 8000):
        self.path = Path(db_path)
        self.log = log
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    # ---------- 连接 ----------

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=False,
            isolation_level=None,  # 手动控制事务，避免隐式 BEGIN 干扰
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # WAL 下 NORMAL 已足够安全
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA wal_autocheckpoint = 512")
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------- 事务 ----------

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """事务上下文。嵌套调用退化为 SAVEPOINT。

        用法：
            with db.transaction() as conn:
                conn.execute(...)
        抛出任何异常都会回滚。
        """
        with self._lock:
            conn = self.conn
            depth = self._tx_depth
            if depth == 0:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                conn.execute(f"SAVEPOINT sp_{depth}")
            self._tx_depth = depth + 1
            try:
                yield conn
            except BaseException:
                try:
                    if depth == 0:
                        conn.execute("ROLLBACK")
                    else:
                        conn.execute(f"ROLLBACK TO SAVEPOINT sp_{depth}")
                except Exception as e:
                    if self.log:
                        self.log.error(f"回滚失败: {e}")
                raise
            else:
                try:
                    if depth == 0:
                        conn.execute("COMMIT")
                    else:
                        conn.execute(f"RELEASE SAVEPOINT sp_{depth}")
                except Exception as e:
                    if self.log:
                        self.log.error(f"提交失败: {e}")
                    raise AimarketError(ErrorCode.STORAGE_ERROR, f"提交事务失败: {e}")
                finally:
                    self._tx_depth = depth

    def in_transaction(self) -> bool:
        return self._tx_depth > 0

    # ---------- 基础 SQL ----------

    def execute(self, sql: str, params=()) -> int:
        """返回影响行数。必须在事务内调用写操作，否则自动开一个。"""
        with self._lock:
            try:
                cur = self.conn.execute(sql, tuple(params))
                return cur.rowcount if cur.rowcount is not None else 0
            except sqlite3.Error as e:
                raise AimarketError(ErrorCode.STORAGE_ERROR, f"SQL 执行失败: {e} | {sql}")

    def executescript(self, script: str) -> None:
        """在当前事务内逐条执行脚本。

        注意：**不能**直接用 sqlite3 的 executescript —— 它会隐式提交当前事务，
        导致迁移无法整体回滚，也会误伤外层事务。这里自己按 `;` 切分后逐条执行。
        """
        with self._lock:
            for statement in split_sql_statements(script):
                try:
                    self.conn.execute(statement)
                except sqlite3.Error as e:
                    raise AimarketError(
                        ErrorCode.STORAGE_ERROR, f"脚本执行失败: {e} | {statement[:120]}"
                    )

    def query_one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                cur = self.conn.execute(sql, tuple(params))
                row = cur.fetchone()
            except sqlite3.Error as e:
                raise AimarketError(ErrorCode.STORAGE_ERROR, f"查询失败: {e} | {sql}")
            return dict(row) if row is not None else None

    def query_all(self, sql: str, params=()) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                cur = self.conn.execute(sql, tuple(params))
                rows = cur.fetchall()
            except sqlite3.Error as e:
                raise AimarketError(ErrorCode.STORAGE_ERROR, f"查询失败: {e} | {sql}")
            return [dict(r) for r in rows]

    def insert(self, table: str, data: Dict[str, Any]) -> int:
        fields = ",".join(data.keys())
        placeholders = ",".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({fields}) VALUES ({placeholders})"
        with self._lock:
            cur = self.conn.execute(sql, tuple(data.values()))
            return int(cur.lastrowid or 0)

    def insert_or_ignore(self, table: str, data: Dict[str, Any]) -> int:
        fields = ",".join(data.keys())
        placeholders = ",".join("?" for _ in data)
        sql = f"INSERT OR IGNORE INTO {table} ({fields}) VALUES ({placeholders})"
        with self._lock:
            cur = self.conn.execute(sql, tuple(data.values()))
            return int(cur.lastrowid or 0)

    def update(self, table: str, data: Dict[str, Any], where: str, params=()) -> int:
        set_clause = ",".join(f"{k} = ?" for k in data.keys())
        sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
        with self._lock:
            cur = self.conn.execute(sql, tuple(data.values()) + tuple(params))
            return cur.rowcount or 0

    def delete(self, table: str, where: str, params=()) -> int:
        with self._lock:
            cur = self.conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(params))
            return cur.rowcount or 0

    def scalar(self, sql: str, params=(), default=None):
        row = self.query_one(sql, params)
        if row is None:
            return default
        return list(row.values())[0]

    def table_exists(self, name: str) -> bool:
        return self.scalar(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ) is not None

    # ---------- 迁移 ----------

    def ensure_migrations_table(self) -> None:
        with self.transaction():
            self.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    checksum    TEXT NOT NULL,
                    applied_at_ms INTEGER NOT NULL
                )"""
            )

    def applied_versions(self) -> Dict[int, str]:
        rows = self.query_all("SELECT version, checksum FROM schema_migrations")
        return {int(r["version"]): str(r["checksum"]) for r in rows}

    def apply_migration(self, version: int, name: str, script: str) -> bool:
        checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
        applied = self.applied_versions()
        if version in applied:
            if applied[version] != checksum:
                if self.log:
                    self.log.warning(
                        f"迁移 {version}({name}) 内容已变更但已应用过，跳过。"
                        f"（如需重建请删除数据库文件）"
                    )
            return False
        try:
            with self.transaction():
                self.executescript(script)
                self.insert(
                    "schema_migrations",
                    {
                        "version": version,
                        "name": name,
                        "checksum": checksum,
                        "applied_at_ms": int(time.time() * 1000),
                    },
                )
        except AimarketError:
            raise
        except Exception as e:
            raise AimarketError(ErrorCode.STORAGE_ERROR, f"迁移 {version}({name}) 失败: {e}")
        if self.log:
            self.log.info(f"已应用数据库迁移 {version}: {name}")
        return True
