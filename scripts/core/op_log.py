# -*- coding: utf-8 -*-
"""操作日志（Operation Log）。

把每次执行的结构化结果落盘，用于：审计、排错、以及「流程挖掘」（对应
Agent Behavior Mining：把智能体活动转成标准 process log）。

同时提供 XES 风格导出，可直接喂给流程挖掘工具（ProM / PM4Py）。
"""
import json
import os
import time


class OperationLog:
    def __init__(self, path=None):
        self.path = path
        self.entries = []

    def record(self, step_index, action, result, duration_ms=0, extra=None):
        """记录一条执行结果。

        result: "success" | "fail" | "skip"
        """
        self.entries.append({
            "seq": len(self.entries) + 1,
            "step": step_index,
            "ts": int(time.time() * 1000),
            "type": action.type,
            "func": action.func,
            "target": action.element_ref or (action.params[0] if action.params else ""),
            "params": action.params,
            "text": action.text,
            "result": result,
            "duration_ms": duration_ms,
            "extra": extra or {},
        })

    def save(self, path=None):
        path = path or self.path
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema": "apc-op-log/v1", "entries": self.entries},
                          f, ensure_ascii=False, indent=2)
        return path

    def to_xes_lines(self):
        """导出 XES 风格（简化）流程日志，便于 process mining。"""
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<log>']
        for e in self.entries:
            lines.append('  <trace>')
            lines.append('    <event>')
            lines.append('      <string key="concept:name" value="%s"/>' % _xml(e["func"]))
            lines.append('      <string key="apc:type" value="%s"/>' % _xml(e["type"]))
            lines.append('      <string key="apc:target" value="%s"/>' % _xml(str(e["target"])))
            lines.append('      <string key="apc:result" value="%s"/>' % _xml(e["result"]))
            lines.append('      <int key="apc:duration" value="%d"/>' % int(e["duration_ms"]))
            lines.append('    </event>')
            lines.append('  </trace>')
        lines.append('</log>')
        return lines

    def save_xes(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.to_xes_lines()) + "\n")
        return path


def _xml(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
