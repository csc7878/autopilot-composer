# -*- coding: utf-8 -*-
"""原子动作 schema。

一条原子动作 = 动词(func) + 目标(element_ref 指向 elements.json) + 参数(params)。
这是把「原始事件流」升级为「操作日志建模」的核心数据结构（对标影刀指令 /
UFO² 的 {verb,target,params} / Agent Behavior Mining 的 process log 事件）。
"""
import uuid
import time

ACTION_TYPES = {"browser", "gui", "cli", "component"}


def new_id(prefix="a"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


class Action:
    def __init__(self, type, func, params=None, element_ref=None, text=None,
                 note="", ts=None, app="", domain="web"):
        if type not in ACTION_TYPES:
            raise ValueError("unknown action type: %s" % type)
        self.type = type
        self.func = func
        self.params = params or []
        self.element_ref = element_ref   # 指向 elements.json 的元素 id
        self.text = text                 # 录入文本（便于审计/流程挖掘）
        self.note = note
        self.app = app
        self.domain = domain
        self.ts = ts or int(time.time() * 1000)
        self.id = new_id()
        self.status = "pending"          # pending | done | error

    def to_dict(self):
        return {
            "id": self.id, "type": self.type, "func": self.func,
            "params": self.params, "element_ref": self.element_ref,
            "text": self.text, "note": self.note, "app": self.app,
            "domain": self.domain, "ts": self.ts, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d):
        # 兼容两种字段名：本技能导出用 "params"，录制器 task_flow.json 用 "args"
        params = d.get("params")
        if params is None:
            params = d.get("args")
        a = cls(d["type"], d["func"], params, d.get("element_ref"),
                d.get("text"), d.get("note", ""), d.get("ts"),
                d.get("app", ""), d.get("domain", "web"))
        a.id = d.get("id", a.id)
        a.status = d.get("status", "pending")
        return a


def actions_to_taskflow(actions):
    """导出 AutoPilot Composer 原生 task_flow.json（兼容旧回放器的 args 结构）。

    含 element_ref 的步骤会附带 'element_ref' 字段；旧版回放器忽略该字段，
    新版（main_task v2）用其解析元素库做多策略定位。
    """
    steps = []
    for a in actions:
        step = {"type": a.type, "func": a.func, "args": list(a.params)}
        if a.element_ref:
            step["element_ref"] = a.element_ref
        if a.note:
            step["note"] = a.note
        steps.append(step)
    return steps
