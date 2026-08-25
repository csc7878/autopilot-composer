# -*- coding: utf-8 -*-
"""Observer：把「原始操作事件」转换为「原子动作」，并自动登记元素库。

这是把用户说的「观察操作日志变动，记录每一步可独立记录的动作」落地的核心层：
  - 导航去重（点击/按键引发的跳转不重复记 open_url）
  - 每个交互元素在进入时登记到 ElementRepository，动作只持有 element_ref
  - 动作 = {type, func, params, element_ref, text, note, ts}

对应 Agent Behavior Mining 的「活动 -> 标准 process log 事件」思想。
"""
from .actions import Action
from .locator import build_web_locators, build_win_locators
from .element_repo import ElementRepository


class Observer:
    def __init__(self, repo, domain="web", preset_repo=None):
        self.repo = repo if repo is not None else ElementRepository()
        self.domain = domain
        self.preset_repo = preset_repo  # 预置元素库（可选）

    def _el_dict(self, ev):
        """从录制事件里抽取元素描述（recorder 端需补齐这些字段）。"""
        return {
            "id": ev.get("el_id"),
            "name": ev.get("el_name"),
            "placeholder": ev.get("el_placeholder"),
            "role": ev.get("el_role"),
            "aria_label": ev.get("el_aria_label"),
            "text": ev.get("el_text") or ev.get("text"),
            "tag": ev.get("tag"),
            "css_path": ev.get("selector"),
        }

    def _match_preset(self, ev):
        """若预置库里存在可命中的，直接复用其 eid。

        命中策略（任一满足即可）：
          1) 当前 domain 与预置 domain 匹配（含通配 *）；
          2) 否则仅当「id 选择器精确匹配」——id 全局唯一，误撞概率极低，
             可用于本地 demo 等无域名场景。
        """
        if not self.preset_repo:
            return None
        dom = self.domain
        for eid, el in self.preset_repo.elements.items():
            locs = {l["strategy"]: l["query"] for l in el.get("locators", [])}
            domain_ok = el.get("domain") in (dom, "*")
            # id 精确匹配：无论 domain，风险极低（id 全局唯一）
            if ev.get("el_id") and locs.get("id") == ("#" + ev["el_id"]):
                return eid
            if not domain_ok:
                continue
            if ev.get("el_name") and locs.get("name") == ("input[name=\"%s\"]" % ev["el_name"]):
                return eid
            if ev.get("el_placeholder") and locs.get("placeholder") == (
                    "input[placeholder=\"%s\"]" % ev["el_placeholder"]):
                return eid
        return None

    def _use_preset(self, eid):
        """把预置元素并入当前 repo（若未并入），并返回其 eid。"""
        if eid not in self.repo.elements and self.preset_repo:
            el = self.preset_repo.get(eid)
            if el:
                self.repo.elements[eid] = el
        return eid

    def _reg(self, ev, kind="web", name_hint=""):
        if kind == "web":
            preset_ref = self._match_preset(ev)
            if preset_ref:
                return self._use_preset(preset_ref)
        el = self._el_dict(ev)
        locators = build_web_locators(el) if kind == "web" else build_win_locators(el)
        if not locators:
            return None
        nm = self._nice_name(el, kind, name_hint)
        return self.repo.register(self.domain, nm, locators, kind=kind)

    def _nice_name(self, el, kind, name_hint=""):
        """生成结构化、易读的元素名（参考 RPA 编辑器：标签前缀 + 语义）。

        例：<input placeholder="请输入用户名"> → 「输入框_请输入用户名」
            <button>登录</button>               → 「按钮_登录」
        这样元素库与回放日志一眼能认，二次编辑也方便。
        """
        tag = (el.get("tag") or "").lower()
        role = el.get("role") or ""
        semantic = (el.get("placeholder") or el.get("aria_label") or el.get("name")
                    or el.get("text") or "").strip()
        if kind == "web":
            if tag in ("input", "textarea"):
                prefix = "输入框"
            elif tag == "select":
                prefix = "下拉框"
            elif tag == "button" or role == "button":
                prefix = "按钮"
            elif tag == "a":
                prefix = "链接"
            elif role:
                prefix = "控件"
            else:
                prefix = "元素"
        else:
            prefix = "控件"
        if semantic:
            # 去掉可能夹带的换行/多余空白，限长避免过长
            semantic = " ".join(semantic.split())[:24]
            return "%s_%s" % (prefix, semantic)
        if name_hint:
            return "%s_%s" % (prefix, name_hint)
        return "%s_%s" % (prefix, (tag or "x"))

    def events_to_actions(self, events):
        actions = []
        last_action_ts = 0
        for ev in events:
            t = ev.get("type")
            ts = ev.get("ts", 0)
            if t in ("click", "keys"):
                last_action_ts = max(last_action_ts, ts)

            if t == "navigate":
                if ev.get("initial") or (ts - last_action_ts) >= 1500:
                    actions.append(Action("browser", "open_url", [ev["url"]],
                                          note="打开页面", ts=ts))
            elif t == "click":
                if "selector" in ev:
                    ref = self._reg(ev, "web")
                    actions.append(Action("browser", "click_elem", [], element_ref=ref,
                                          note="点击", ts=ts, app="网页"))
                elif "x" in ev:
                    actions.append(Action("gui", "click_at", [ev["x"], ev["y"]],
                                          note="点击", ts=ts, app=ev.get("app", "桌面"), domain="win"))
            elif t == "change":
                ref = self._reg(ev, "web")
                val = ev.get("value", "")
                actions.append(Action("browser", "input_text", [val], element_ref=ref,
                                      text=val, note="录入", ts=ts))
            elif t == "hover":
                if "selector" in ev:
                    ref = self._reg(ev, "web")
                    actions.append(Action("browser", "hover", [], element_ref=ref, note="悬停", ts=ts))
                elif "x" in ev:
                    actions.append(Action("gui", "hover_at", [ev["x"], ev["y"]], note="悬停", ts=ts,
                                          app=ev.get("app", "桌面"), domain="win"))
            elif t == "drag":
                if "from_sel" in ev:
                    actions.append(Action("browser", "drag", [ev.get("from_sel"), ev.get("to_sel")],
                                          note="拖拽", ts=ts))
                elif "from" in ev:
                    f, to = ev["from"], ev["to"]
                    actions.append(Action("gui", "drag_move", [f[0], f[1], to[0], to[1]],
                                          note="拖拽", ts=ts, app=ev.get("app", "桌面"), domain="win"))
            elif t == "keys":
                if ev.get("text"):
                    actions.append(Action("gui", "input_text", [ev["text"]], text=ev["text"],
                                          note="录入", ts=ts, app=ev.get("app", "桌面"), domain="win"))
                elif ev.get("keys"):
                    if self.domain == "win" or ev.get("app"):
                        actions.append(Action("gui", "press_keys", [ev["keys"]], note="按键", ts=ts,
                                              app=ev.get("app", "桌面"), domain="win"))
                    else:
                        actions.append(Action("browser", "key_press", [ev["keys"]], note="按键", ts=ts))
            elif t == "upload":
                ref = self._reg(ev, "web")
                actions.append(Action("browser", "upload_file", [ev.get("files", [])],
                                      element_ref=ref, note="上传", ts=ts))
            elif t == "focus":
                actions.append(Action("gui", "open_software", [ev.get("exe", "")],
                                      note="启动/切换应用：%s" % ev.get("app", ""),
                                      ts=ts, app=ev.get("app", "桌面"), domain="win"))
            elif t == "double_click":
                actions.append(Action("gui", "double_click_at", [ev["x"], ev["y"]],
                                      note="双击", ts=ts, app=ev.get("app", "桌面"), domain="win"))
            elif t == "right_click":
                actions.append(Action("gui", "right_click_at", [ev["x"], ev["y"]],
                                      note="右键", ts=ts, app=ev.get("app", "桌面"), domain="win"))
        return actions
