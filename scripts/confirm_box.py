# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 录制时操作确认的 GUI 弹窗

录制过程中，每捕获到一个原子操作后，弹出一个系统级窗口，让用户当场决定：
  - 「记录」：把这一步写入 SOP（task_flow.json）+ 元素库（elements.json）
  - 「跳过」：放弃这一步（不写入）

设计要点：
  - 跨平台：Windows / macOS / Linux 用 tkinter（Python 标准库自带）
  - 非阻塞式内部实现：后台线程跑 tk mainloop，主线程可等待结果
  - 支持 timeout：超时（默认 8s）自动「跳过」，避免阻塞自动化卡死
  - 显示动作摘要（动词 + 目标描述），让用户快速判断
"""
import os
import sys
import time
import threading
import json


def _build_summary(action):
    """把一条录制事件转成人类可读的摘要。"""
    t = action.get("type")
    if t == "navigate":
        return "导航到页面\n%s" % (action.get("url", ""))
    if t == "click":
        return "点击元素\n%s" % (action.get("text") or action.get("selector"))
    if t == "change":
        return "输入文本\n%s →「%s」" % (action.get("selector"), action.get("value", ""))
    if t == "drag":
        return "拖拽\n%s → %s" % (action.get("from_sel"), action.get("to_sel"))
    if t == "hover":
        return "悬停\n%s" % (action.get("selector"))
    if t == "keys":
        return "按键\n%s" % ("+".join(action.get("keys", [])))
    if t == "upload":
        return "上传文件\n%s ← %s" % (action.get("selector"), ", ".join(action.get("files", [])))
    return "操作: %s" % t


class ConfirmBox:
    """一次性的「是否记录这一步」弹窗。"""

    def __init__(self, timeout=8):
        self.timeout = timeout
        self.result = None
        self._lock = threading.Event()

    def ask(self, action, title="记录此操作？"):
        """弹出确认框，返回 True（记录）/ False（跳过）。"""
        summary = _build_summary(action)
        try:
            return self._run_tk(summary, title)
        except Exception as e:
            # tkinter 不可用时降级为命令行确认，保证流程不崩
            print("\n⚠️ 无法弹出 GUI（%s），改为命令行确认：" % e)
            print("  将要记录的操作：%s" % summary.replace("\n", " "))
            got = input("  记录此步? [y/N]: ").strip().lower()
            return got in ("y", "yes", "是", "确认")

    def _run_tk(self, summary, title):
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        try:
            root.wm_attributes("-toolwindow", True)
        except Exception:
            pass

        # 布局
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="AutoPilot 录制", font=("Arial", 12, "bold")).pack(anchor="w")
        ttk.Label(frame, text=summary, font=("Consolas", 11),
                  wraplength=360, justify="left").pack(pady=10, anchor="w")

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=6, anchor="e")

        result = {"val": False}

        def on_record():
            result["val"] = True
            root.destroy()

        def on_skip():
            result["val"] = False
            root.destroy()

        ttk.Button(btn_frame, text="记录 ✓", command=on_record, width=10).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="跳过 ✗", command=on_skip, width=10).pack(side="left")

        # 倒计时标签
        remain = {"t": self.timeout}

        def tick():
            remain["t"] -= 1
            if root.winfo_exists():
                if remain["t"] <= 0:
                    on_skip()
                else:
                    lbl.config(text="%d 秒后自动跳过" % remain["t"])
                    root.after(1000, tick)

        lbl = ttk.Label(frame, text="%d 秒后自动跳过" % self.timeout, foreground="#888")
        lbl.pack(anchor="e", pady=(2, 0))
        root.after(1000, tick)

        # 居中
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry("+%d+%d" % (x, y))
        root.resizable(False, False)

        # 用 after 在超时后强制关闭，避免无限等待
        root.after(self.timeout * 1000, lambda: on_skip() if root.winfo_exists() else None)

        root.mainloop()
        return result["val"]


def confirm_action(action, timeout=8):
    """便捷函数：录制时调用，返回是否记录。"""
    return ConfirmBox(timeout=timeout).ask(action)


if __name__ == "__main__":
    a = {"type": "change", "selector": "#kw", "value": "autopilot"}
    print("用户选择记录?", confirm_action(a))
