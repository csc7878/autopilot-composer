#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
AutoPilot Composer —— 桌面应用操作录制器（Record 模式 · 原生 Windows 应用）

录制你在任意 Windows 软件里的鼠标 / 键盘操作，自动标注当前应用
（企业微信 / 微信 / 钉钉 / WPS / 金蝶 / 用友 等，按 exe 识别），停止后导出：

  1) desktop_flow.json   —— 可作为 AutoPilot Composer 的 gui 类型流程播放
  2) recorded_desktop.py —— 独立 pyautogui 脚本（python recorded_desktop.py 直接跑）

支持的捕获：
  - 点击 / 双击 / 右键
  - 拖拽（按住移动超过阈值）
  - 悬停（指针停留 > 500ms）
  - 键盘：组合键（Ctrl+C 等）+ 连续输入的文本（自动聚合成一段文字）
  - 应用切换：焦点进入新应用即记录 open_software(exe)

依赖（仅 Windows 运行时需要）：pynput、pywin32、psutil
"""

import sys
import os
import json
import time
import threading

# 这些依赖只在 Windows 真机录制时用到；沙箱/离线测试仅调用纯转换函数，故惰性导入。
try:
    from pynput import mouse as _pymouse, keyboard as _pykeyboard
    from pynput.mouse import Button as _Button
    HAS_PYNPUT = True
except Exception:
    HAS_PYNPUT = False

# 已知应用 exe -> 中文名（按需增补）
APP_MAP = {
    "WXWork.exe": "企业微信",
    "WeChat.exe": "微信",
    "DingTalk.exe": "钉钉",
    "wps.exe": "WPS 文字",
    "wpspdf.exe": "WPS PDF",
    "et.exe": "WPS 表格",
    "wpp.exe": "WPS 演示",
    "excel.exe": "Microsoft Excel",
    "winword.exe": "Microsoft Word",
    "powerpnt.exe": "Microsoft PowerPoint",
    "msaccess.exe": "Microsoft Access",
    "outlook.exe": "Microsoft Outlook",
    "k3cloud.exe": "金蝶云星空",
    "kingdee.exe": "金蝶",
    "yonyou.exe": "用友",
    "ufida.exe": "用友(U8)",
}


def _app_name(exe_basename):
    return APP_MAP.get(exe_basename, exe_basename or "未知应用")


# ---------------------------------------------------------------------------
# 录制器本体
# ---------------------------------------------------------------------------
class DesktopRecorder:
    def __init__(self):
        self.events = []
        self._running = False
        self._lock = threading.Lock()
        self._last_focus_exe = None
        self._down = None          # (x, y, button_name) 按下时的状态
        self._last_click = None    # (ts, x, y) 用于双击判定
        self._hover_timer = None
        self._hover_pos = None
        self._typed = ""           # 待聚合成的文本
        self._mods = set()         # 当前按下的修饰键
        self._listeners = []

    # ---- 窗口信息 ----
    def _get_foreground(self):
        """返回 {'app','exe','title'}；无依赖时返回空。"""
        try:
            import win32gui
            import win32process
            import psutil
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = psutil.Process(pid).exe()
            exe_base = os.path.basename(exe)
            return {"app": _app_name(exe_base), "exe": exe, "title": title}
        except Exception:
            return {"app": "未知应用", "exe": "", "title": ""}

    def _emit(self, obj):
        obj["ts"] = int(time.time() * 1000)
        info = self._get_foreground()
        obj["app"] = info["app"]
        obj["exe"] = info["exe"]
        with self._lock:
            self.events.append(obj)

    def _flush_typed(self):
        if self._typed:
            self._emit({"type": "keys", "text": self._typed, "keys": []})
            self._typed = ""

    # ---- 鼠标回调 ----
    def _on_move(self, x, y):
        if self._down:
            return  # 拖拽中
        # 悬停检测
        if self._hover_pos == (x, y):
            return
        self._hover_pos = (x, y)
        if self._hover_timer:
            self._hover_timer.cancel()
        self._hover_timer = threading.Timer(0.5, self._emit_hover, args=(x, y))
        self._hover_timer.daemon = True
        self._hover_timer.start()

    def _emit_hover(self, x, y):
        self._hover_timer = None
        self._emit({"type": "hover", "x": x, "y": y})

    def _on_click(self, x, y, button, pressed):
        if self._hover_timer:
            self._hover_timer.cancel()
            self._hover_timer = None
        btn = "left" if button == _Button.left else ("right" if button == _Button.right else "middle")
        if pressed:
            self._down = (x, y, btn)
            return
        # 松开
        if not self._down:
            return
        sx, sy, sbtn = self._down
        self._down = None
        dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
        self._flush_typed()
        if dist > 12 and sbtn == "left":
            self._emit({"type": "drag", "from": [sx, sy], "to": [x, y]})
            self._last_click = None
            return
        # 双击判定
        now = time.time()
        if (sbtn == "left" and self._last_click and
                now - self._last_click[0] < 0.35 and
                abs(x - self._last_click[1]) < 8 and abs(y - self._last_click[2]) < 8):
            # 撤销上一次 click，改记 double_click
            with self._lock:
                if self.events and self.events[-1].get("type") == "click":
                    self.events.pop()
            self._emit({"type": "double_click", "x": x, "y": y})
            self._last_click = None
            return
        if sbtn == "right":
            self._emit({"type": "right_click", "x": x, "y": y})
        else:
            self._emit({"type": "click", "x": x, "y": y})
            self._last_click = (now, x, y)

    # ---- 键盘回调 ----
    def _key_name(self, key):
        """把 pynput key 转成可读 token（pyautogui 兼容命名）。

        pynput 的特殊键 str 形如 'Key.enter'/'Key.up'/'Key.f5'/...，而播放端
        gui_engine.press_keys 直接透传给 pyautogui，pyautogui 只认小写键名
        （enter/up/space/esc/win/f5 等），故这里统一输出 pyautogui 命名。
        """
        k = str(key)
        if hasattr(key, "char") and key.char and len(key.char) == 1:
            return key.char
        name_map = {
            "Key.enter": "enter", "Key.tab": "tab", "Key.space": "space",
            "Key.backspace": "backspace", "Key.delete": "delete", "Key.esc": "esc",
            "Key.ctrl": "ctrl", "Key.ctrl_l": "ctrl", "Key.ctrl_r": "ctrl",
            "Key.alt": "alt", "Key.alt_l": "alt", "Key.alt_r": "alt",
            "Key.shift": "shift", "Key.shift_l": "shift", "Key.shift_r": "shift",
            "Key.cmd": "win", "Key.cmd_l": "win", "Key.cmd_r": "win",
            "Key.home": "home", "Key.end": "end",
            "Key.pageup": "pageup", "Key.pagedown": "pagedown",
            "Key.insert": "insert",
        }
        if k in name_map:
            return name_map[k]
        if k.startswith("Key."):
            rest = k[4:]
            if rest in ("up", "down", "left", "right"):
                return rest  # pyautogui 方向键用小写 up/down/left/right
            if rest.startswith("f") and rest[1:].isdigit():
                return rest  # pyautogui 功能键用小写 f1..f12
            return rest
        return k

    def _on_press(self, key):
        try:
            name = self._key_name(key)
        except Exception:
            return
        low = name.lower()
        if low in ("ctrl", "alt", "shift", "win", "meta"):
            self._mods.add(low)
            return
        # 文本输入：可见单字符
        if len(name) == 1 and name.isprintable():
            self._typed += name
            return
        # 特殊键 / 组合键
        self._flush_typed()
        combo = sorted(self._mods) + [name]
        self._emit({"type": "keys", "keys": combo, "text": ""})

    def _on_release(self, key):
        try:
            name = self._key_name(key).lower()
        except Exception:
            return
        if name in ("ctrl", "alt", "shift", "meta"):
            self._mods.discard(name)

    # ---- 应用焦点监听（独立线程轮询） ----
    def _focus_watch(self):
        while self._running:
            info = self._get_foreground()
            exe = info.get("exe")
            if exe and exe != self._last_focus_exe:
                self._last_focus_exe = exe
                self._flush_typed()
                self._emit({"type": "focus", "app": info["app"],
                            "exe": exe, "title": info["title"]})
            time.sleep(0.6)

    # ---- 开始 / 停止 ----
    def start(self):
        if not HAS_PYNPUT:
            raise RuntimeError("未安装 pynput，无法在桌面录制。请先 pip install pynput pywin32 psutil")
        self._running = True
        self._last_focus_exe = None
        ml = _pymouse.Listener(on_move=self._on_move, on_click=self._on_click)
        kl = _pykeyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        ml.start(); kl.start()
        self._listeners = [ml, kl]
        threading.Thread(target=self._focus_watch, daemon=True).start()

    def stop(self):
        self._running = False
        for l in self._listeners:
            try:
                l.stop()
            except Exception:
                pass
        self._flush_typed()
        return list(self.events)


# ---------------------------------------------------------------------------
# 事件 → 可复用脚本 的转换
# ---------------------------------------------------------------------------
def events_to_gui_taskflow(events):
    """导出 AutoPilot Composer 原生 gui 流程步骤。"""
    steps = []
    for ev in events:
        t = ev.get("type")
        if t == "focus":
            steps.append({"type": "gui", "func": "open_software", "args": [ev.get("exe", "")]})
        elif t == "click":
            steps.append({"type": "gui", "func": "click_at", "args": [ev["x"], ev["y"]]})
        elif t == "double_click":
            steps.append({"type": "gui", "func": "double_click_at", "args": [ev["x"], ev["y"]]})
        elif t == "right_click":
            steps.append({"type": "gui", "func": "right_click_at", "args": [ev["x"], ev["y"]]})
        elif t == "hover":
            steps.append({"type": "gui", "func": "hover_at", "args": [ev["x"], ev["y"]]})
        elif t == "drag":
            f, to = ev["from"], ev["to"]
            steps.append({"type": "gui", "func": "drag_move",
                          "args": [f[0], f[1], to[0], to[1]]})
        elif t == "keys":
            if ev.get("text"):
                steps.append({"type": "gui", "func": "input_text", "args": [ev["text"]]})
            elif ev.get("keys"):
                steps.append({"type": "gui", "func": "press_keys", "args": [ev["keys"]]})
    return steps


PYAUTOGUI_TEMPLATE = """# AutoPilot Composer 录制的桌面脚本（独立运行，不依赖本技能）
# 运行前安装依赖： pip install pyautogui pyperclip
# 运行： python recorded_desktop.py
import pyautogui
import pyperclip
import time

pyautogui.PAUSE = 1
pyautogui.FAILSAFE = False

%s
"""


def events_to_pyautogui(events):
    """导出独立 pyautogui 脚本的步骤行。"""
    lines = []
    for ev in events:
        t = ev.get("type")
        if t == "focus":
            lines.append('os_startfile_if_needed(%r)  # 应用: %s' %
                         (ev.get("exe", ""), ev.get("app", "")))
        elif t == "click":
            lines.append("pyautogui.click(%d, %d)" % (ev["x"], ev["y"]))
        elif t == "double_click":
            lines.append("pyautogui.doubleClick(%d, %d)" % (ev["x"], ev["y"]))
        elif t == "right_click":
            lines.append("pyautogui.rightClick(%d, %d)" % (ev["x"], ev["y"]))
        elif t == "hover":
            lines.append("pyautogui.moveTo(%d, %d)" % (ev["x"], ev["y"]))
        elif t == "drag":
            f, to = ev["from"], ev["to"]
            lines.append("pyautogui.moveTo(%d, %d); pyautogui.mouseDown(); "
                         "pyautogui.moveTo(%d, %d, duration=0.5); pyautogui.mouseUp()"
                         % (f[0], f[1], to[0], to[1]))
        elif t == "keys":
            if ev.get("text"):
                lines.append('pyperclip.copy(%r); pyautogui.hotkey("ctrl", "v")  # 输入文本'
                             % ev["text"])
            elif ev.get("keys"):
                keys = ev["keys"]
                if len(keys) == 1:
                    lines.append('pyautogui.press(%r)' % keys[0])
                else:
                    lines.append('pyautogui.hotkey(%s)' % ", ".join('%r' % k for k in keys))
    return lines


def write_pyautogui(path, lines):
    import os as _os
    body = "\n".join(lines) if lines else "# （未录制到操作）"
    header = ('def os_startfile_if_needed(path):\n'
              '    import os\n'
              '    if path:\n'
              '        try: os.startfile(path)\n'
              '        except Exception: pass\n'
              '    time.sleep(2)\n\n')
    with open(path, "w", encoding="utf-8") as f:
        f.write(PYAUTOGUI_TEMPLATE % (header + body))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="AutoPilot Composer 桌面录制器")
    ap.add_argument("--out", default="desktop_flow.json", help="导出的 gui task_flow.json")
    ap.add_argument("--py", default="recorded_desktop.py", help="导出的独立 pyautogui 脚本")
    args = ap.parse_args()

    rec = DesktopRecorder()
    rec.start()
    try:
        input("🔴 桌面录制中… 正常操作各类软件，回到这里输入任意内容并回车停止：\n> ")
    except KeyboardInterrupt:
        pass
    events = rec.stop()

    if not events:
        print("⚠️ 没有录制到任何操作。")
        return

    tf = events_to_gui_taskflow(events)
    py_lines = events_to_pyautogui(events)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False, indent=2)
    write_pyautogui(args.py, py_lines)

    print("✅ 已录制 %d 个桌面动作，导出两份文件：" % len(events))
    print("   - %s  （%d 步，gui 类型，可并入主流程播放）" % (args.out, len(tf)))
    print("   - %s  （python 直接运行）" % args.py)


if __name__ == "__main__":
    main()
