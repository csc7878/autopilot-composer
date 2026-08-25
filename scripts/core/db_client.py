# -*- coding: utf-8 -*-
"""DB Client - T1 直连层 SQL 数据库客户端。

支持 pyodbc / pymysql / sqlite3 三种驱动，统一接口。
参数化查询防注入 + 连接池 + 事务 + read_only + 审计日志 + 敏感字段脱敏。

Action 形态：
    {"type":"sql","func":"query","args":["get_order_count"],
     "credential_ref":"kingdee_db"}
"""
import json
import time
import logging
from .credential_manager import resolve_credential
from .db_security import mask_sensitive, audit_log

logger = logging.getLogger(__name__)

# 驱动惰性加载
_drivers = {}


def _get_driver(db_type):
    if db_type in _drivers:
        return _drivers[db_type]
    if db_type == "pyodbc":
        try:
            import pyodbc
            _drivers["pyodbc"] = pyodbc
            return pyodbc
        except ImportError:
            return None
    elif db_type == "pymysql":
        try:
            import pymysql
            _drivers["pymysql"] = pymysql
            return pymysql
        except ImportError:
            return None
    elif db_type == "sqlite3":
        import sqlite3
        _drivers["sqlite3"] = sqlite3
        return sqlite3
    return None


class DbClient:
    """SQL 数据库客户端。"""

    def __init__(self, config=None):
        """
        config: {
            "db_type": "pyodbc|pymysql|sqlite3",
            "host": "...", "port": 3306,
            "database": "...", "username": "...", "password": "...",
            "driver": "ODBC Driver 17 for SQL Server",  # pyodbc only
            "read_only": true,
            "pool_size": 5
        }
        """
        self.config = config or {}
        self.db_type = self.config.get("db_type", "sqlite3")
        self.read_only = self.config.get("read_only", False)
        self.pool_size = self.config.get("pool_size", 5)
        self._pool = []
        self._pool_lock = None

    @classmethod
    def from_credential(cls, credential_ref):
        """从凭证管理器加载连接配置。"""
        cred = resolve_credential(credential_ref)
        if not cred:
            raise RuntimeError("凭证未找到: %s" % credential_ref)
        return cls(cred)

    def _connect(self):
        driver = _get_driver(self.db_type)
        if not driver:
            raise RuntimeError("数据库驱动未安装: %s" % self.db_type)
        if self.db_type == "pyodbc":
            conn_str = self._build_odbc_conn_str()
            return driver.connect(conn_str)
        elif self.db_type == "pymysql":
            return driver.connect(
                host=self.config.get("host", "localhost"),
                port=self.config.get("port", 3306),
                user=self.config.get("username", ""),
                password=self.config.get("password", ""),
                database=self.config.get("database", ""),
                charset="utf8mb4",
                cursorclass=driver.cursors.DictCursor,
            )
        elif self.db_type == "sqlite3":
            db_path = self.config.get("database", ":memory:")
            conn = driver.connect(db_path)
            conn.row_factory = driver.Row
            return conn

    def _build_odbc_conn_str(self):
        parts = []
        drv = self.config.get("driver", "ODBC Driver 17 for SQL Server")
        parts.append("DRIVER={%s}" % drv)
        if self.config.get("host"):
            parts.append("SERVER=%s" % self.config["host"])
        if self.config.get("database"):
            parts.append("DATABASE=%s" % self.config["database"])
        if self.config.get("username"):
            parts.append("UID=%s" % self.config["username"])
        if self.config.get("password"):
            parts.append("PWD=%s" % self.config["password"])
        return ";".join(parts) + ";"

    def query(self, sql, params=None):
        """执行参数化查询，返回行列表。"""
        if self.read_only and not sql.strip().upper().startswith("SELECT"):
            raise RuntimeError("read_only 模式禁止写操作: %s" % sql[:60])
        conn = self._connect()
        try:
            cursor = conn.cursor()
            t0 = time.time()
            cursor.execute(sql, params or ())
            rows = cursor.fetchall()
            elapsed = int((time.time() - t0) * 1000)
            result = [self._row_to_dict(r) for r in rows]
            result = mask_sensitive(result, self.config.get("sensitive_fields", []))
            audit_log(self.db_type, "query", sql, params, elapsed, len(result))
            return {"rc": 0, "rows": result, "row_count": len(result), "elapsed_ms": elapsed}
        except Exception as e:
            logger.error("SQL 查询失败: %s" % e)
            return {"rc": -1, "error": str(e)}
        finally:
            conn.close()

    def execute(self, sql, params=None):
        """执行写操作（INSERT/UPDATE/DELETE），返回受影响行数。"""
        if self.read_only:
            raise RuntimeError("read_only 模式禁止写操作")
        conn = self._connect()
        try:
            cursor = conn.cursor()
            t0 = time.time()
            cursor.execute(sql, params or ())
            conn.commit()
            elapsed = int((time.time() - t0) * 1000)
            affected = cursor.rowcount
            audit_log(self.db_type, "execute", sql, params, elapsed, affected)
            return {"rc": 0, "rows_affected": affected, "elapsed_ms": elapsed}
        except Exception as e:
            conn.rollback()
            logger.error("SQL 执行失败: %s" % e)
            return {"rc": -1, "error": str(e)}
        finally:
            conn.close()

    def transaction(self, statements):
        """执行事务（多条 SQL 原子提交）。"""
        if self.read_only:
            raise RuntimeError("read_only 模式禁止写操作")
        conn = self._connect()
        try:
            cursor = conn.cursor()
            t0 = time.time()
            total_affected = 0
            for stmt in statements:
                sql = stmt.get("sql", "")
                params = stmt.get("params", ())
                cursor.execute(sql, params)
                total_affected += cursor.rowcount
            conn.commit()
            elapsed = int((time.time() - t0) * 1000)
            audit_log(self.db_type, "transaction",
                      "; ".join(s.get("sql", "")[:30] for s in statements),
                      None, elapsed, total_affected)
            return {"rc": 0, "rows_affected": total_affected, "elapsed_ms": elapsed}
        except Exception as e:
            conn.rollback()
            logger.error("事务失败: %s" % e)
            return {"rc": -1, "error": str(e)}
        finally:
            conn.close()

    def _row_to_dict(self, row):
        if isinstance(row, dict):
            return row
        try:
            cols = [d[0] for d in row.cursor.description]
            return dict(zip(cols, row))
        except Exception:
            return {"value": str(row)}


def execute(action, registry=None, credential_ref=None):
    """按 sql action 执行，返回结果 dict。"""
    func = action.func
    if func == "query":
        sql = action.params[0] if action.params else ""
        params = action.params[1] if len(action.params) > 1 else None
        cred_ref = credential_ref or getattr(action, "credential_ref", None)
        client = DbClient.from_credential(cred_ref) if cred_ref else DbClient()
        return client.query(sql, params)
    elif func == "execute":
        sql = action.params[0] if action.params else ""
        params = action.params[1] if len(action.params) > 1 else None
        cred_ref = credential_ref or getattr(action, "credential_ref", None)
        client = DbClient.from_credential(cred_ref) if cred_ref else DbClient()
        return client.execute(sql, params)
    elif func == "transaction":
        stmts = action.params[0] if action.params else []
        cred_ref = credential_ref or getattr(action, "credential_ref", None)
        client = DbClient.from_credential(cred_ref) if cred_ref else DbClient()
        return client.transaction(stmts)
    return {"rc": -3, "error": "unknown sql func: %s" % func}
