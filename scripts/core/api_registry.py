# -*- coding: utf-8 -*-
"""API Registry - T1 直连层 API 模板注册表。

存储 API 模板（method/base_url/path/headers/body/auth/assertions），
录制时由 network_capture 自动生成，也可手动注册。

模板结构：
{
    "query_orders": {
        "method": "GET",
        "base_url": "https://api.kingdee.com",
        "path": "/k3cloud/orders/list",
        "headers": {"Content-Type": "application/json"},
        "auth": {"type": "bearer"},
        "query_params": {"page": 1, "size": 20},
        "timeout": 30,
        "max_retry": 2,
        "assertions": [
            {"type": "status", "expected": 200},
            {"type": "body_contains", "expected": "orders"}
        ]
    }
}
"""
import json
import os


class ApiRegistry:
    """API 模板注册表，支持文件持久化。"""

    def __init__(self, path=None):
        self.path = path
        self.templates = {}
        if path and os.path.exists(path):
            self.load(path)

    def register(self, name, template):
        self.templates[name] = template
        return name

    def get(self, name):
        return self.templates.get(name)

    def list_names(self):
        return sorted(self.templates.keys())

    def remove(self, name):
        return self.templates.pop(name, None) is not None

    def save(self, path=None):
        path = path or self.path
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "templates": self.templates},
                          f, ensure_ascii=False, indent=2)
        return path

    def load(self, path):
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.templates = data.get("templates", {})
        except Exception:
            pass

    def infer_name_from_url(self, method, url):
        """从 URL 推断 API 名称（录制时自动命名）。

        例：GET https://api.kingdee.com/k3cloud/orders/list -> GET_orders_list
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return "%s_root" % method.lower()
        parts = path.replace("/", "_").replace("-", "_")
        return "%s_%s" % (method.lower(), parts)


# 全局单例
_default = None


def get_registry(path=None):
    global _default
    if _default is None:
        _default = ApiRegistry(path)
    return _default
