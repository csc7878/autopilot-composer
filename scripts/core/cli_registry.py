# -*- coding: utf-8 -*-
"""CLI Registry - T1 直连层命令行模板注册表。

存储 CLI 命令模板（subprocess/COM/PowerShell/SDK），白名单模式——
回放器只执行注册表中的模板，不执行任意代码（安全考量）。

模板结构：
{
    "export_kingdee_report": {
        "executor": "subprocess",      # subprocess | com | powershell | sdk
        "command": "kingdee-cli export --type {report_type} --date {date}",
        "params": ["report_type", "date"],
        "timeout": 120,
        "cwd": "C:\\kingdee\\bin"
    }
}
"""
import json
import os


class CliRegistry:
    """CLI 命令模板注册表。"""

    def __init__(self, path=None):
        self.path = path
        self.templates = {}
        if path and os.path.exists(path):
            self.load(path)

    def register(self, name, template):
        """注册一个 CLI 模板。"""
        self.templates[name] = template
        return name

    def get(self, name):
        return self.templates.get(name)

    def list_names(self):
        return sorted(self.templates.keys())

    def remove(self, name):
        return self.templates.pop(name, None) is not None

    def render(self, name, params):
        """渲染模板：把 params 填入 {param} 占位符。"""
        template = self.templates.get(name)
        if not template:
            raise RuntimeError("CLI 模板未注册: %s" % name)
        command = template.get("command", "")
        for k, v in (params or {}).items():
            command = command.replace("{%s}" % k, str(v))
        return template, command

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


# 全局单例
_default = None


def get_registry(path=None):
    global _default
    if _default is None:
        _default = CliRegistry(path)
    return _default
