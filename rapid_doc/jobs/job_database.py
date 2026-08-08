"""异步 Job 服务的 MySQL 8 数据库连接与初始化工具。"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .job_config import JobSettings


SCHEMA_PATH = Path(__file__).with_name("job_schema_mysql.sql")


class DatabaseError(Exception):
    """Job 存储层统一暴露的数据库错误。"""


class DatabaseRow(dict[str, Any]):
    """同时支持字段名和旧代码使用的数字下标。"""

    def __getitem__(self, key: object) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class DatabaseCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self) -> DatabaseRow | None:
        row = self._cursor.fetchone()
        return None if row is None else DatabaseRow(row)

    def fetchall(self) -> list[DatabaseRow]:
        return [DatabaseRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class DatabaseConnection:
    """对 PyMySQL 做一层轻量适配，保留现有 JobStore 的 execute 用法。"""

    def __init__(self, raw_connection: Any) -> None:
        self._connection = raw_connection

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] = ()) -> DatabaseCursor:
        normalized_sql = _normalize_sql(sql)
        cursor = self._connection.cursor()
        cursor.execute(normalized_sql, parameters)
        return DatabaseCursor(cursor)

    def executemany(self, sql: str, parameters: Any) -> DatabaseCursor:
        cursor = self._connection.cursor()
        cursor.executemany(_normalize_sql(sql), parameters)
        return DatabaseCursor(cursor)

    def begin(self) -> None:
        self._connection.begin()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def connect_database(settings: JobSettings) -> DatabaseConnection:
    """连接 MySQL；密码只传给驱动，不写入日志。"""

    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:  # pragma: no cover - 由部署依赖安装保证
        raise DatabaseError("缺少 MySQL 驱动 pymysql，请安装 rapid-doc[mysql]") from exc

    if settings.database_backend != "mysql":
        raise DatabaseError("RAPID_DOC_DB_BACKEND 必须设置为 mysql")
    try:
        raw_connection = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseError(f"连接 MySQL 失败: {exc}") from exc
    return DatabaseConnection(raw_connection)


def initialize_database(settings: JobSettings) -> None:
    """初始化 MySQL 表结构；DDL 使用 IF NOT EXISTS，重复启动安全。"""

    connection = connect_database(settings)
    try:
        for statement in SCHEMA_PATH.read_text(encoding="utf-8").split(";\n"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
        connection.execute(
            "INSERT IGNORE INTO schema_migrations(version, description, applied_at) "
            "VALUES (?, ?, ?)",
            (1, "MySQL 8 初始 Job Schema", int(time.time())),
        )
    finally:
        connection.close()


def _normalize_sql(sql: str) -> str:
    normalized = sql.strip()
    normalized = re.sub(r"^BEGIN IMMEDIATE$", "START TRANSACTION", normalized)
    normalized = re.sub(r"^INSERT OR IGNORE INTO", "INSERT IGNORE INTO", normalized)
    return normalized.replace("?", "%s")
