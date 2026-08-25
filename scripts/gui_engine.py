import pyautogui
import time
import pyperclip
import os

pyautogui.PAUSE = 1
pyautogui.FAILSAFE = False


class GuiAutomation:
    # ---------- 启动软件 ----------
    def open_software(self, path=""):
        """启动 / 切换到目标软件。

        优化（v3.3.3）：回放时若目标 exe 已有前台窗口，直接把它带到前台，
        不再新开一个进程 —— 彻底消除「回放越跑窗口越多」的问题。
        仅当找不到已运行实例时才用 os.startfile 新开。
        """
        if path and self._focus_existing_window(path):
            return
        os.startfile(path)
        time.sleep(2)

    def _focus_existing_window(self, path):
        """若已存在该 exe 的可见窗口，带到前台并返回 True。找不到/异常则 False。"""
        try:
            import win32gui
            import win32con
            import win32process
            import psutil
        except Exception:
            return False
        target = os.path.basename(path).lower()
        if not target:
            return False
        hits = []

        def _enum(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                exe = psutil.Process(pid).name().lower()
            except Exception:
                return
            if exe == target:
                hits.append(hwnd)

        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            return False
        if not hits:
            return False
        hwnd = hits[0]
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(1)
            return True
        except Exception:
            return False

    def open_software_by_exe(self, exe_path=""):
        """按可执行文件路径启动（桌面录制器会记录前台窗口的 exe）。"""
        return self.open_software(exe_path)

    # ---------- 按坐标操作（桌面录制器产出） ----------
    def click_at(self, x, y):
        pyautogui.click(int(x), int(y))

    def double_click_at(self, x, y):
        pyautogui.doubleClick(int(x), int(y))

    def right_click_at(self, x, y):
        pyautogui.rightClick(int(x), int(y))

    def hover_at(self, x, y):
        pyautogui.moveTo(int(x), int(y))

    def drag_move(self, x1, y1, x2, y2):
        pyautogui.moveTo(x1, y1)
        pyautogui.mouseDown()
        pyautogui.moveTo(x2, y2, duration=0.5)
        pyautogui.mouseUp()

    def scroll_at(self, x, y, clicks=3):
        pyautogui.moveTo(int(x), int(y))
        pyautogui.scroll(clicks)

    # ---------- 文本与按键 ----------
    def input_text(self, text):
        """用剪贴板粘贴（对中文/特殊字符最稳）。"""
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")

    def type_text(self, text):
        """直接键盘录入（适合密码框、纯 ASCII 场景）。"""
        pyautogui.write(text)

    def hot_key(self, *keys):
        pyautogui.hotkey(*keys)

    def press_keys(self, keys):
        """按键 / 组合键。keys 可为列表(如 ['ctrl','c']) 或字符串('ctrl+c')。"""
        if isinstance(keys, str):
            parts = [k.strip() for k in keys.split("+") if k.strip()]
        else:
            parts = list(keys)
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
