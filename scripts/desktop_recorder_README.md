# AutoPilot Composer 桌面应用录制器（desktop_recorder.py）

录制你在**任意 Windows 软件**里的鼠标 / 键盘操作，自动标注当前应用（企业微信 / 微信 / 钉钉 / WPS / 金蝶 / 用友 等，按 exe 识别），停止后导出可复用的脚本。与网页录制器（`recorder.py`）配合，可用 `record_session.py` 合并成统一流程。

## 它能录制什么

| 你的操作 | 导出为 | 说明 |
|----------|--------|------|
| 切换到某软件 | `open_software(exe)` | 焦点进入新应用即记录其 exe 路径 |
| 单击 | `click_at(x,y)` | 屏幕绝对坐标 |
| 双击 | `double_click_at(x,y)` | 自动判定（两次左键 < 350ms 同位置） |
| 右键 | `right_click_at(x,y)` | — |
| 拖拽 | `drag_move(x1,y1,x2,y2)` | 按住移动超过阈值 |
| 悬停 | `hover_at(x,y)` | 指针停留 > 500ms |
| 连续打字 | `input_text(文本)` | 自动聚合成一段文字（剪贴板粘贴，中文也稳） |
| 组合键（Ctrl+C 等） | `press_keys(["ctrl","c"])` | 含修饰键时记为组合键 |
| 应用切换 | 见 `open_software` | 自动标注应用名（企业微信/WPS…） |

## 产物

| 产物 | 用途 | 怎么跑 |
|------|------|--------|
| `desktop_flow.json` | AutoPilot Composer 的 `gui` 类型流程 | 并入 `task_flow.json` 用 `main_task.py` 播放 |
| `recorded_desktop.py` | 独立 pyautogui 脚本 | `python recorded_desktop.py` |

## 前置依赖（仅 Windows 运行时需要）

```bat
pip install pynput pywin32 psutil
```

- `pynput`：全局鼠标/键盘监听
- `pywin32`：获取前台窗口句柄与标题
- `psutil`：由窗口 pid 解析 exe 路径

## 用法

```bat
cd scripts
.venv\Scripts\activate

python desktop_recorder.py                       :: 回车开始，操作软件，回车停止
python desktop_recorder.py --out df.json --py d.py
```

## 应用识别（APP_MAP）

脚本内置常见 exe 名映射：

| exe | 识别为 |
|-----|--------|
| WXWork.exe | 企业微信 |
| WeChat.exe | 微信 |
| DingTalk.exe | 钉钉 |
| wps.exe / et.exe / wpspdf.exe / wpp.exe | WPS 文字 / 表格 / PDF / 演示 |
| excel.exe / winword.exe / powerpnt.exe | Office 套件 |
| k3cloud.exe / kingdee.exe | 金蝶 |
| yonyou.exe / ufida.exe | 用友 |

未列出的 exe 会直接显示其文件名。如需更多，在 `desktop_recorder.py` 顶部的 `APP_MAP` 增补即可。

## 复用与修订

- 合并进网页流程：把 `desktop_flow.json` 的步骤与网页 `task_flow.json` 手动拼接，或用 `record_session.py` 自动交错合并。
- 坐标稳定性：回放依赖录制时的屏幕分辨率与窗口位置。若窗口会变动，建议把 `click_at` 坐标改为基于窗口相对坐标，或改用 `click_icon` 图像识别。

## 已知限制

- 原生应用没有 CSS 选择器，定位以**屏幕坐标 + 前台 exe** 为准，抗界面变化能力弱于网页录制。
- 仅支持 Windows（依赖 `win32gui` / `psutil`）。
- 文件对话框内选择文件、UAC 弹窗等系统级交互难以精确捕获，复杂场景建议配合手动补步骤。
- 沙箱 / 无 GUI 环境无法实跑（监听需要真实桌面），但导出/转换逻辑可离线验证。
