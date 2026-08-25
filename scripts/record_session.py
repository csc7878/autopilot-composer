#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
AutoPilot Composer —— 合并录制会话（网页 + 桌面 同时录制）【三合一升级版】

把「浏览器网页」与「本机软件（企业微信/微信/钉钉/WPS/金蝶/用友…）」里的操作一起录制，
按真实时间顺序合并，一键导出统一产物：

  1) task_flow.json      —— 原子动作流程（含 element_ref 指向元素库），main_task.py 直接播放
  2) elements.json       —— 独立元素库（多策略定位器电池，改一处全局生效）
  3) operation_log.json  —— 操作日志模板（回放时填充，用于审计/流程挖掘）
  4) SOP.md              —— 人读版操作手册（标注每步所属应用 + 元素名）
  （兼容导出）recorded_flow.js / recorded_desktop.py —— 独立 Playwright / pyautogui 脚本

设计要点（对标最新 GUI Agent / RPA 论文）：
  - 原子动作 = verb + element_ref + params（而非内联脆弱选择器）
  - 元素定位走多策略定位器电池（id/name/placeholder/role_name/testid/css）
  - 高频动作可封装为 components/ 下的复用组件

用法：
  python record_session.py                  # 网页+桌面同时录
  python record_session.py --desktop-only   # 只录桌面
  python record_session.py --web-only       # 只录网页
  python record_session.py --port 9222
"""

import sys
import os
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from recorder import (WebRecorder, events_to_playwright, write_playwright)
from desktop_recorder import (DesktopRecorder, events_to_pyautogui, write_pyautogui)
from core.observer import Observer
from core.element_repo import ElementRepository
from core.actions import actions_to_taskflow, describe_action
from core.op_log import OperationLog
from core.api_registry import ApiRegistry


# ---------------------------------------------------------------------------
# 合并录制去重：丢弃落在调试 Chrome 窗口内的桌面坐标点击
# （网页侧已由 WebRecorder 捕获同一动作，桌面侧再记一份是冗余，且回放时
#  会按屏幕坐标盲点，窗口位置一变就点错。v3.3.3 优化）
# ---------------------------------------------------------------------------
def _chrome_window_rects():
    """返回当前可见 chrome.exe 窗口的矩形列表 [(l,t,r,b), ...]。无 win32 则返回 []。"""
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return []
    rects = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() == "chrome.exe":
                rects.append(win32gui.GetWindowRect(hwnd))
        except Exception:
            return

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception:
        return rects
    return rects


def _in_rects(x, y, rects):
    for (l, t, r, b) in rects:
        if l <= x <= r and t <= y <= b:
            return True
    return False


def drop_desktop_inside_chrome(events):
    """合并录制时，剔除落在 Chrome 窗口内的桌面点击/悬停/拖拽（网页侧已覆盖）。"""
    rects = _chrome_window_rects()
    if not rects:
        return events, 0
    out, dropped = [], 0
    for ev in events:
        t = ev.get("type")
        pt = None
        if t in ("click", "hover", "double_click", "right_click"):
            pt = (ev.get("x"), ev.get("y"))
        elif t == "drag":
            f = ev.get("from")
            if f:
                pt = (f[0], f[1])
        if pt and _in_rects(pt[0], pt[1], rects):
            dropped += 1
            continue
        out.append(ev)
    return out, dropped


# ---------------------------------------------------------------------------
# SOP 文本生成（带元素库引用 + 流程架构图 + 按应用分组）
# ---------------------------------------------------------------------------
def _sop_lines(actions, repo):
    lines = ["# AutoPilot Composer 录制操作手册（SOP）", "",
             "> 本手册由录制器自动生成，步骤按真实操作时间排序。",
             "> 元素定位全部登记在 `elements.json`，页面改版时只需改元素库。",
             "> 想临时跳过某步：编辑 `task_flow.json` 把该步 `\"enabled\": true` 改成 `false`。", ""]

    web_n = sum(1 for a in actions if a.type == "browser")
    desk_n = len(actions) - web_n

    # ---- 一、流程架构图 ----
    lines += [
        "## 一、流程架构图",
        "",
        "```mermaid",
        "flowchart LR",
        "    A[开始录制] --> B{录制模式}",
        "    B -->|网页| C[调试 Chrome<br/>捕获点击/输入/悬停]",
        "    B -->|桌面| D[监听鼠标键盘<br/>+ 焦点切换]",
        "    C --> E[WebRecorder]",
        "    D --> F[DesktopRecorder]",
        "    E --> G[Observer 转原子动作<br/>+ 登记元素库]",
        "    F --> G",
        "    G --> H[按时间合并<br/>去重 / 过滤噪声]",
        "    H --> I[task_flow.json<br/>+ elements.json<br/>+ SOP.md]",
        "    I --> J[main_task.py 回放]",
        "    J --> K{含 element_ref?}",
        "    K -->|是| L[元素库多策略定位]",
        "    K -->|否| M[内联选择器]",
        "    L --> N(执行)",
        "    M --> N",
        "```",
        "",
        "```",
        "录制 ──┬─ 网页(调试Chrome) ─┐",
        "       │                    ├─► Observer 原子动作+元素库 ─► 合并去重 ─► task_flow.json / elements.json / SOP.md ─► main_task.py 回放",
        "       └─ 桌面(鼠标键盘)  ──┘",
        "```",
        "",
        # ---- 二、步骤总览 ----
        "## 二、步骤总览",
        "",
        "- 总步数：**%d**（网页 %d / 桌面 %d）" % (len(actions), web_n, desk_n),
        "- 元素库：**%d** 个元素（见第四节速查）" % len(repo.elements),
        "",
        # ---- 三、操作步骤（按应用分组） ----
        "## 三、操作步骤（按应用分组）",
        "",
    ]

    cur_group = None
    n = 0
    for a in actions:
        grp = ("🌐 网页（Chrome 调试浏览器）" if a.type == "browser"
               else (a.app or "🖥️ 桌面"))
        if grp != cur_group:
            cur_group = grp
            lines += ["### %s" % grp, ""]
            n = 0
        n += 1
        lines.append("%d. %s" % (n, describe_action(a, repo)))

    # ---- 四、元素库速查 ----
    lines += ["",
              "## 四、元素库速查（改页面只需改这里）",
              "",
              "| 元素名 | 类型 | 主定位器 |",
              "| --- | --- | --- |"]
    for el in repo.elements.values():
        locs = el.get("locators", [])
        primary = locs[0] if locs else {}
        strat = primary.get("strategy", "")
        q = primary.get("query", "")
        if len(q) > 46:
            q = q[:43] + "..."
        lines.append("| %s | %s | `%s:%s` |" % (el.get("name", ""), el.get("kind", ""), strat, q))

    # ---- 五、二次编辑指南 ----
    lines += ["",
              "## 五、二次编辑指南（像 RPA 编辑器一样改流程）",
              "",
              "- **禁用某步**：打开 `task_flow.json`，把对应步骤的 `\"enabled\": true` 改为 `false`，回放自动跳过。",
              "- **改文本 / 坐标**：直接改该步 `args` 数组里的值（如手机号、坐标 x/y）。",
              "- **改元素定位**：编辑 `elements.json` 里对应元素的 `locators`，回放走元素库多策略定位，页面改版只改这里。",
              "- **跨应用切换**：`open_software` 步骤回放时先聚焦已有窗口、不再新开（v3.3.3 优化）。",
              ""]
    return lines


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AutoPilot Composer 合并录制会话（三合一版）")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--web-only", action="store_true")
    ap.add_argument("--desktop-only", action="store_true")
    ap.add_argument("--out", default="task_flow.json")
    ap.add_argument("--elements", default="elements.json")
    ap.add_argument("--oplog", default="operation_log.json")
    ap.add_argument("--js", default="recorded_flow.js")
    ap.add_argument("--py", default="recorded_desktop.py")
    ap.add_argument("--sop", default="SOP.md")
    ap.add_argument("--components-dir", default=os.path.join(HERE, "components"))
    args = ap.parse_args()

    do_web = not args.desktop_only
    do_desk = not args.web_only

    web_events, desk_events = [], []
    web_rec = None
    desk_rec = None

    if do_web:
        try:
            web_rec = WebRecorder(port=args.port)
            web_rec.connect()
            web_rec.start()
        except Exception as e:
            print("⚠️ 网页录制器未能启动（%s）。仅记录桌面部分。" % e)
            do_web = False
    if do_desk:
        try:
            desk_rec = DesktopRecorder()
            desk_rec.start()
        except Exception as e:
            print("⚠️ 桌面录制器未能启动（%s）。仅记录网页部分。" % e)
            do_desk = False
    if not do_web and not do_desk:
        print("❌ 网页与桌面录制器都无法启动，退出。")
        return

    try:
        input("🔴 录制中… 同时操作网页和各类软件，回到这里输入任意内容并回车停止：\n> ")
    except KeyboardInterrupt:
        pass

    if do_web:
        web_events = web_rec.stop()
        web_rec.close()
    if do_desk:
        desk_events = desk_rec.stop()
        # 合并录制去重：网页+桌面同时录时，落在 Chrome 窗口内的桌面坐标操作
        # 与网页侧重复，丢弃之（仅当确实在录网页时才去重）。
        if do_web and web_rec is not None:
            desk_events, dropped = drop_desktop_inside_chrome(desk_events)
            if dropped:
                print("   ↳ 合并去重：丢弃 %d 个落在 Chrome 窗口内的桌面点击（网页侧已覆盖）"
                      % dropped)

    # ---- 统一转换为原子动作 + 自动登记元素库 ----
    repo = ElementRepository()
    web_obs = Observer(repo, domain="web")
    desk_obs = Observer(repo, domain="win")
    web_actions = web_obs.events_to_actions(web_events) if do_web else []
    desk_actions = desk_obs.events_to_actions(desk_events) if do_desk else []
    all_actions = sorted(web_actions + desk_actions, key=lambda a: a.ts)

    if not all_actions:
        print("⚠️ 没有录制到任何操作。")
        return

    task_flow = actions_to_taskflow(all_actions, repo)

    # T1 直连层：把 Network 捕获的 API 请求转为模板并关联 t1_ref
    api_registry = None
    if do_web and web_rec and web_rec.network_capture and web_rec.network_capture.requests:
        api_registry = ApiRegistry()
        web_rec.network_capture.to_api_templates(api_registry)
        api_path = os.path.join(os.path.dirname(os.path.abspath(args.out)),
                                "api_registry.json")
        api_registry.save(api_path)
        # 给 browser 步骤关联 t1_ref
        from recorder import _attach_t1_refs
        _attach_t1_refs(task_flow, web_events,
                        web_rec.network_capture.get_api_events(), api_registry)

    # ---- 写出新四件套 ----
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(task_flow, f, ensure_ascii=False, indent=2)
    repo.save(args.elements)
    op = OperationLog()
    op.save(args.oplog)
    sop = _sop_lines(all_actions, repo)
    with open(args.sop, "w", encoding="utf-8") as f:
        f.write("\n".join(sop) + "\n")

    # ---- 兼容导出（独立脚本） ----
    if do_web and web_events:
        write_playwright(args.js, events_to_playwright(web_events))
    if do_desk and desk_events:
        write_pyautogui(args.py, events_to_pyautogui(desk_events))

    print("✅ 录制完成，共 %d 个原子动作（网页 %d / 桌面 %d），登记 %d 个元素："
          % (len(all_actions), len(web_events), len(desk_events), len(repo.elements)))
    print("   - %s   （原子动作流程，含 element_ref + t1_ref，main_task.py 播放）" % args.out)
    print("   - %s   （独立元素库，多策略定位器）" % args.elements)
    print("   - %s   （操作日志模板，回放时填充）" % args.oplog)
    print("   - %s   （人读 SOP 手册）" % args.sop)
    if api_registry and api_registry.templates:
        print("   - api_registry.json  （%d 个 API 模板，T1 直连层）" % len(api_registry.templates))
    if do_web and web_events:
        print("   - %s   （网页 Playwright 脚本，兼容）" % args.js)
    if do_desk and desk_events:
        print("   - %s   （桌面 pyautogui 脚本，兼容）" % args.py)


if __name__ == "__main__":
    main()
