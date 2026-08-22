# -*- coding: utf-8 -*-
"""定位器（locator）策略。

把一次捕获到的元素描述，转化为「多策略定位器电池」，回放时按稳定性优先级
依次尝试（对标 Playwright 官方建议：面向用户的语义定位器最抗改版；
对标 Windows UIA：automationId 优先）。

Web 策略优先级（越靠前越稳）：
    id > name > placeholder > role_name > testid > css

桌面（Windows UIA）策略优先级：
    automation_id > control_type_name > name > css
"""

# Web 定位策略优先级（面向用户的语义定位器最抗改版）
WEB_PRIORITY = ["id", "name", "placeholder", "role_name", "testid", "css"]
# 桌面（Windows UIA）定位策略优先级
WIN_PRIORITY = ["automation_id", "control_type_name", "name", "css"]


def _esc(v):
    """转义 CSS 属性选择器里的双引号与反斜杠。"""
    if v is None:
        return ""
    return v.replace("\\", "\\\\").replace('"', '\\"')


def build_web_locators(el):
    """el: 从 DOM 捕获到的元素描述（id/name/placeholder/role/aria_label/text/css_path）。
    返回 [{"strategy":..., "query":...}, ...] 定位器电池。"""
    locators = []
    el_id = el.get("id")
    name = el.get("name") or el.get("aria_label") or ""
    ph = el.get("placeholder") or ""
    role = el.get("role") or ""
    text = (el.get("text") or "").strip()
    if el_id:
        locators.append({"strategy": "id", "query": "#" + el_id})
    if name:
        locators.append({"strategy": "name", "query": '[name="%s"]' % _esc(name)})
    if ph:
        locators.append({"strategy": "placeholder", "query": '[placeholder="%s"]' % _esc(ph)})
    if role and (el.get("aria_label") or text):
        label = el.get("aria_label") or text[:30]
        locators.append({"strategy": "role_name",
                         "query": '[role="%s"][aria-label="%s"]' % (_esc(role), _esc(label))})
    tid = el.get("testid") or el.get("data_testid") or ""
    if tid:
        locators.append({"strategy": "testid", "query": '[data-testid="%s"]' % _esc(tid)})
    if el.get("css_path"):
        locators.append({"strategy": "css", "query": el["css_path"]})
    return locators


def build_win_locators(el):
    """el: 从桌面 UIA 捕获到的元素描述（automation_id/control_type/name/css_path）。"""
    locators = []
    aid = el.get("automation_id")
    ct = el.get("control_type") or ""
    nm = el.get("name") or ""
    if aid:
        locators.append({"strategy": "automation_id", "query": aid})
    if ct and nm:
        locators.append({"strategy": "control_type_name", "query": "%s|%s" % (ct, nm)})
    elif nm:
        locators.append({"strategy": "name", "query": nm})
    if el.get("css_path"):
        locators.append({"strategy": "css", "query": el["css_path"]})
    return locators


def pick_locator(locators, priority):
    """从定位器电池中按 priority 选第一个可用的定位器。"""
    by_strategy = {l["strategy"]: l["query"] for l in locators}
    for strat in priority:
        if strat in by_strategy and by_strategy[strat]:
            return {"strategy": strat, "query": by_strategy[strat]}
    return None


def is_xpath(selector):
    """粗略判断选择器是否为 XPath。"""
    if not selector:
        return False
    s = selector.strip()
    return s.startswith("/") or s.startswith("//") or "::" in s
