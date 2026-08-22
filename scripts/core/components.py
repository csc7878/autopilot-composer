# -*- coding: utf-8 -*-
"""复用组件（Reusable Components）。

把高频小动作固化成「参数化组件」，下次直接引用而非重录（对标 OS-Copilot 的
自学习技能 / 影刀组件库）。组件可以是：

  1) 参数化代码模板（python / javascript），用 {{param}} 占位符注入参数；
  2) 一张「子流程」表（一组原子动作），由 main_task 递归执行。

组件存放目录： scripts/components/  （每个组件一个 .json）

组件 JSON 结构：
{
  "name": "login_crm",
  "lang": "python",                 // python | javascript | flow
  "params": ["url", "user", "pwd"],
  "desc": "登录 CRM 系统",
  "body": "import sys\\nprint('login', '{{url}}', '{{user}}')"   // 或 flow: [...]
}
"""
import os
import json

COMPONENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "components")


def set_component_dir(path):
    global COMPONENT_DIR
    COMPONENT_DIR = path


def list_components():
    if not os.path.isdir(COMPONENT_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(COMPONENT_DIR)):
        if fn.endswith(".json"):
            try:
                d = json.load(open(os.path.join(COMPONENT_DIR, fn), encoding="utf-8"))
                out.append(d)
            except Exception:
                pass
    return out


def get_component(name):
    p = os.path.join(COMPONENT_DIR, name + ".json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return None


def render(body, kwargs):
    """把参数填进 {{param}} 占位符。"""
    for k, v in (kwargs or {}).items():
        body = body.replace("{{%s}}" % k, str(v))
    return body


def save_component(comp):
    """把组件定义写入 components/ 目录。"""
    os.makedirs(COMPONENT_DIR, exist_ok=True)
    name = comp.get("name")
    if not name:
        raise ValueError("component 缺少 name")
    path = os.path.join(COMPONENT_DIR, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(comp, f, ensure_ascii=False, indent=2)
    return path
