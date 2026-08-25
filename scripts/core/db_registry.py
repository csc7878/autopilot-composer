# -*- coding: utf-8 -*-
"""DB Registry - T1 直连层数据库连接配置注册表。

存储数据库连接配置（不含密码，密码从 credential_manager 引用）。
"""
import json
import os


class DbRegistry:
    """数据库连接配置注册表。"""

    def __init__(self, path=None):
        self.path = path
        self.connections = {}
        if path and os.path.exists(path):
            self.load(path)

    def register(self, name, config):
        """注册一个数据库连接配置。

        config: {
            "db_type": "pyodbc|pymysql|sqlite3",
            "host": "...", "port": 3306,
            "database": "...", "username": "...",
            "credential_ref": "kingdee_db_pwd",  # 密码从这里取
            "read_only": true,
            "sensitive_fields": ["password", "id_card"]
        }
        """
        self.connections[name] = config
        return name

    def get(self, name):
        return self.connections.get(name)

    def list_names(self):
        return sorted(self.connections.keys())

    def remove(self, name):
        return self.connections.pop(name, None) is not None

    def save(self, path=None):
        path = path or self.path
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "connections": self.connections},
                          f, ensure_ascii=False, indent=2)
        return path

    def load(self, path):
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.connections = data.get("connections", {})
        except Exception:
            pass


# 全局单例
_default = None


def get_registry(path=None):
    global _default
    if _default is None:
        _default = DbRegistry(path)
    return _default
