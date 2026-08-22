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
from core.actions import actions_to_taskflow
from core.op_log import OperationLog


# ---------------------------------------------------------------------------
# SOP 文本生成（带元素库引用）
# ---------------------------------------------------------------------------
def _sop_lines(actions, repo):
    lines = ["# AutoPilot Composer 录制操作手册（SOP）", "",
             "> 本手册由录制器自动生成，步骤按真实操作时间排序。",
             "> 元素定位全部登记在 `elements.json`，页面改版时只需改元素库。", ""]
    n = 0
    for a in actions:
        n += 1
        func = a.func
        params = a.params
        app = a.app or ""
        el_name = ""
        if a.element_ref:
            el = repo.get(a.element_ref)
            if el:
                el_name = el["name"]
        if func == "open_url":
            desc = "打开网页：%s" % params[0]
        elif func == "click_elem":
            desc = "点击元素【%s】" % (el_name or "未命名")
        elif func == "input_text":
            desc = "录入文本：%r  → 【%s】" % (params[0] if params else "", el_name or "未命名")
        elif func == "drag":
            desc = "拖拽：%s → %s" % (params[0], params[1])
        elif func == "hover":
            desc = "悬停：【%s】" % (el_name or "未命名")
        elif func == "key_press":
            desc = "按键：%s" % ("+".join(params[0]) if params else "")
        elif func == "upload_file":
            desc = "上传文件：（%s）" % (", ".join(params[0]) if params and params[0] else "")
        elif func == "open_software":
            desc = "启动/切换应用：%s" % (params[0] or "（未知路径）")
        elif func in ("click_at", "double_click_at", "right_click_at", "hover_at"):
            label = {"click_at": "点击坐标", "double_click_at": "双击坐标",
                     "right_click_at": "右键坐标", "hover_at": "悬停坐标"}[func]
            desc = "%s：(%s, %s)" % (label, params[0], params[1])
        elif func == "drag_move":
            desc = "拖拽坐标：(%s,%s)→(%s,%s)" % (params[0], params[1], params[2], params[3])
        elif func == "press_keys":
            desc = "按键：%s" % ("+".join(params[0]) if params else "")
        else:
            desc = "%s(%s)" % (func, params)
        zone = "【%s】" % app if app else ""
        lines.append("%d. %s%s" % (n, zone, desc))
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

    task_flow = actions_to_taskflow(all_actions)

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
    print("   - %s   （原子动作流程，含 element_ref，main_task.py 播放）" % args.out)
    print("   - %s   （独立元素库，多策略定位器）" % args.elements)
    print("   - %s   （操作日志模板，回放时填充）" % args.oplog)
    print("   - %s   （人读 SOP 手册）" % args.sop)
    if do_web and web_events:
        print("   - %s   （网页 Playwright 脚本，兼容）" % args.js)
    if do_desk and desk_events:
        print("   - %s   （桌面 pyautogui 脚本，兼容）" % args.py)


if __name__ == "__main__":
    main()
