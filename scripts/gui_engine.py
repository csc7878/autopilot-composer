import pyautogui
import time
import pyperclip
import os

pyautogui.PAUSE = 1
pyautogui.FAILSAFE = False


class GuiAutomation:
    # ---------- 启动软件 ----------
    def open_software(self, path=""):
        os.startfile(path)
        time.sleep(2)

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
