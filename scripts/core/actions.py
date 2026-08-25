# -*- coding: utf-8 -*-
"""原子动作 schema。

一条原子动作 = 动词(func) + 目标(element_ref 指向 elements.json) + 参数(params)。
这是把「原始事件流」升级为「操作日志建模」的核心数据结构（对标影刀指令 /
UFO² 的 {verb,target,params} / Agent Behavior Mining 的 process log 事件）。
"""
import uuid
import time

ACTION_TYPES = {"browser", "gui", "cli", "component", "api", "sql"}


def new_id(prefix="a"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


class Action:
    def __init__(self, type, func, params=None, element_ref=None, text=None,
                 note="", ts=None, app="", domain="web",
                 tier=None, credential_ref=None, t1_ref=None):
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
        self.tier = tier                 # T1/T2/T3/T4 直连层标注
        self.credential_ref = credential_ref  # 凭证引用（不存明文凭证）
        self.t1_ref = t1_ref             # 录制时 Network 捕获的关联 T1 路径
        self.ts = ts or int(time.time() * 1000)
        self.id = new_id()
        self.status = "pending"          # pending | done | error

    def to_dict(self):
        return {
            "id": self.id, "type": self.type, "func": self.func,
            "params": self.params, "element_ref": self.element_ref,
            "text": self.text, "note": self.note, "app": self.app,
            "domain": self.domain, "ts": self.ts, "status": self.status,
            "tier": self.tier, "credential_ref": self.credential_ref,
            "t1_ref": self.t1_ref,
        }

    @classmethod
    def from_dict(cls, d):
        # 兼容两种字段名：本技能导出用 "params"，录制器 task_flow.json 用 "args"
        params = d.get("params")
        if params is None:
            params = d.get("args")
        a = cls(d["type"], d["func"], params, d.get("element_ref"),
                d.get("text"), d.get("note", ""), d.get("ts"),
                d.get("app", ""), d.get("domain", "web"),
                d.get("tier"), d.get("credential_ref"), d.get("t1_ref"))
        a.id = d.get("id", a.id)
        a.status = d.get("status", "pending")
        return a


def _clean_keys(keys):
    """把控制字符（Ctrl+字母产生的 \\x03 等）还原成可读字母，避免日志出现乱码。"""
    out = []
    for k in keys:
        if isinstance(k, str) and len(k) == 1 and 0 < ord(k) < 0x20:
            out.append(chr(ord(k) + 0x40))   # \x03 -> 'C', \x16 -> 'V' ...
        else:
            out.append(k)
    return out


def describe_action(a, repo=None):
    """生成一步动作的可读描述（RPA 编辑器风格：动作 + 目标元素 + 参数）。

    同时用于 task_flow.json 的 `comment` 字段与 SOP.md 的步骤文案，保证两处一致。
    """
    func = a.func
    p = a.params
    el_name = ""
    if a.element_ref and repo:
        el = repo.get(a.element_ref)
        if el:
            el_name = " ".join(str(el.get("name", "")).split())  # 折叠换行/多余空白
    if func == "open_url":
        return "打开网页 %s" % (p[0] if p else "")
    if func == "click_elem":
        return "点击【%s】" % (el_name or "元素")
    if func == "input_text":
        txt = p[1] if len(p) > 1 else (p[0] if p else "")
        return "录入「%s」→【%s】" % (txt, el_name or "元素")
    if func == "hover":
        return "悬停【%s】" % (el_name or "元素")
    if func == "key_press":
        ks = _clean_keys(p[0]) if p and p[0] else []
        return "网页按键 %s" % ("+".join(ks) if ks else "")
    if func == "upload_file":
        return "上传文件 %s" % (",".join(p[0]) if p and p[0] else "")
    if func == "drag":
        return "拖拽 %s → %s" % (p[0], p[1])
    if func == "open_software":
        import os as _os
        return "切换/启动 %s" % (_os.path.basename(p[0]) if p else "应用")
    if func in ("click_at", "double_click_at", "right_click_at", "hover_at"):
        label = {"click_at": "点击坐标", "double_click_at": "双击坐标",
                 "right_click_at": "右键坐标", "hover_at": "悬停坐标"}[func]
        return "%s(%s, %s)" % (label, p[0], p[1])
    if func == "drag_move":
        return "拖拽坐标(%s,%s)→(%s,%s)" % (p[0], p[1], p[2], p[3])
    if func == "press_keys":
        ks = _clean_keys(p[0]) if p and p[0] else []
        return "按键 %s" % ("+".join(ks) if ks else "")
    # ---- T1 直连层动作描述 ----
    if func == "call_api":
        return "API 调用 %s" % (p[0] if p else "")
    if func == "run_com":
        return "COM 自动化 %s.%s" % (p[0] if len(p) > 0 else "", p[1] if len(p) > 1 else "")
    if func == "run_ps":
        return "PowerShell %s" % (str(p[0])[:40] if p else "")
    if func == "run_template":
        return "CLI 模板 %s" % (p[0] if p else "")
    if func in ("query", "execute", "transaction"):
        label = {"query": "SQL 查询", "execute": "SQL 执行",
                 "transaction": "SQL 事务"}[func]
        sql = p[0] if p else ""
        if isinstance(sql, str):
            sql = sql.strip().split("\n")[0][:40]
        return "%s: %s" % (label, sql)
    return "%s(%s)" % (func, p)


def actions_to_taskflow(actions, repo=None):
    """导出 AutoPilot Composer 原生 task_flow.json（兼容旧回放器的 args 结构）。

    v3.3.3 增强：每步附带更友好的字段，便于像 RPA 编辑器一样阅读与二次修改：
      - element_ref : 指向 elements.json 的元素 id（优先用元素库多策略定位）
      - app         : 所属应用/分区（网页 / 某桌面软件），便于分组
      - note        : 简短动作词（点击/录入/启动…）
      - comment     : 完整人读描述（动作 + 目标元素 + 参数）
      - enabled     : 是否启用（置 false 可临时跳过该步，无需删行）
    旧版回放器忽略新增字段，新版（main_task）会读取 enabled 支持单步禁用。
    """
    steps = []
    for a in actions:
        step = {"type": a.type, "func": a.func, "args": list(a.params)}
        if a.element_ref:
            step["element_ref"] = a.element_ref
        if a.app:
            step["app"] = a.app
        if a.note:
            step["note"] = a.note
        if a.tier:
            step["tier"] = a.tier
        if a.credential_ref:
            step["credential_ref"] = a.credential_ref
        if a.t1_ref:
            step["t1_ref"] = a.t1_ref
        step["comment"] = describe_action(a, repo)
        step["enabled"] = True
        steps.append(step)
    return steps
