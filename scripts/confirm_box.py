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


# ==================================================================
# v3.5.0：通用多选项确认框（供「置信度驱动的自适应介入」使用）
# ==================================================================

def ask_choice(message, title="需要你确认", options=None, timeout=60,
               default_index=0, detail_lines=None, headline=None):
    """弹出多选项确认框，返回所选选项的 value。

    与 ConfirmBox（录制时「记录/跳过」）的区别：本函数面向**回放时的自适应
    介入**——当某一步置信度偏低时，把「为什么低」摊开给用户看，由他决定
    继续 / 跳过 / 暂停，而不是盲目重试到崩。

    参数
    ----
    message      : 主提示（一般是这一步将要做什么）
    options      : [{"label": "继续执行", "value": "continue"}, ...]
    detail_lines : 归因说明，逐行展示（如置信度的加减项）
    headline     : 顶部大字（如「置信 2.10/5 偏低」）
    timeout      : 秒；超时自动选 default_index 对应的项
    default_index: 默认项下标（注意：把「最保守」的选项配合适的默认值）
    """
    options = options or [{"label": "继续", "value": "continue"}]
    detail_lines = detail_lines or []
    try:
        return _run_choice_tk(message, title, options, timeout,
                              default_index, detail_lines, headline)
    except Exception as e:
        # 无 GUI 环境降级为命令行确认，保证不因为弹不出窗而卡死
        print("\n" + "=" * 60)
        print("⚠️ 无法弹出 GUI 确认框（%s），改为命令行确认：" % e)
        if headline:
            print("   %s" % headline)
        print("   %s" % message)
        for ln in detail_lines:
            print("   %s" % ln)
        for i, op in enumerate(options):
            mark = "*" if i == default_index else " "
            print("   %s[%d] %s" % (mark, i + 1, op.get("label")))
        try:
            raw = input("   请选择序号（回车=默认，%d 秒后自动继续）: " % timeout).strip()
        except Exception:
            raw = ""
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1].get("value")
        return options[default_index].get("value")


def _run_choice_tk(message, title, options, timeout, default_index,
                   detail_lines, headline):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    try:
        root.wm_attributes("-toolwindow", True)
    except Exception:
        pass

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="AutoPilot 回放确认",
              font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
    if headline:
        ttk.Label(frame, text=headline, font=("Microsoft YaHei", 13, "bold"),
                  foreground="#c0392b").pack(anchor="w", pady=(6, 2))
    ttk.Label(frame, text=message, font=("Microsoft YaHei", 10),
              wraplength=430, justify="left").pack(anchor="w", pady=(4, 6))

    if detail_lines:
        box = tk.Text(frame, height=min(10, max(3, len(detail_lines) + 1)),
                      width=54, wrap="word", font=("Consolas", 9),
                      relief="solid", borderwidth=1, background="#f7f7f7")
        box.insert("1.0", "\n".join(detail_lines))
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, pady=(0, 8))

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(anchor="e", pady=(2, 4))

    chosen = {"v": options[default_index].get("value")}
    closed = {"done": False}

    def pick(op):
        chosen["v"] = op.get("value")
        closed["done"] = True
        root.destroy()

    for op in options:
        ttk.Button(btn_frame, text=op.get("label", "?"), width=12,
                   command=lambda o=op: pick(o)).pack(side="left", padx=4)

    lbl = ttk.Label(frame, text="%d 秒后自动选择：%s"
                    % (timeout, options[default_index].get("label", "")),
                    foreground="#888")
    lbl.pack(anchor="e")

    remain = {"t": int(timeout)}

    def tick():
        remain["t"] -= 1
        if not root.winfo_exists():
            return
        if remain["t"] <= 0:
            pick(options[default_index])
        else:
            lbl.config(text="%d 秒后自动选择：%s"
                       % (remain["t"], options[default_index].get("label", "")))
            root.after(1000, tick)

    root.after(1000, tick)

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry("+%d+%d" % (max(0, x), max(0, y)))
    root.resizable(False, False)
    root.mainloop()
    return chosen["v"]


if __name__ == "__main__":
    a = {"type": "change", "selector": "#kw", "value": "autopilot"}
    print("用户选择记录?", confirm_action(a))
    print("多选项测试 ->", ask_choice(
        "步骤 12：点击【登录】按钮",
        headline="置信 1.90/5 偏低",
        detail_lines=["[基准 1.8] T4 路径（屏幕坐标，最脆弱）",
                      "[-1.20] 有 3 个元素都匹配，存在点错的风险",
                      "[-0.40] 最近有 1 次未命中记录"],
        options=[{"label": "继续执行", "value": "continue"},
                 {"label": "跳过这步", "value": "skip"},
                 {"label": "暂停任务", "value": "pause"}],
        timeout=15, default_index=2))

