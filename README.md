# AutoPilot Composer

> 桌面 GUI（pyautogui）+ 浏览器 CDP 双引擎 RPA 框架，支持百级长流程、意外中断**断点续跑**、自动重试、运行日志、后台静默运行。

把「打开软件 → 点点鼠标 → 浏览器填表 → 截图留痕」这样的重复流程写进 `task_flow.json`，一键自动跑；中途崩了也能从断点继续，不用从头再来。

## ✨ 特性

- **双引擎**：桌面 `pyautogui` 引擎 + 浏览器 `CDP` 引擎，覆盖 GUI 与网页混合流程
- **断点续跑**：出错停在断点，修复后从当前步骤继续，不重头跑
- **自动重试**：单步失败按 `max_retry` 重试，超过则暂停并写错误断点
- **运行日志**：每步执行细节写入 `run_log.log`
- **后台静默**：支持 `pythonw` / `run_silent.bat` 无窗口运行
- **真实环境验证**：已在 Chrome 151 + Windows 桌面端到端跑通（见下方「本地示例」）

## 🧱 架构

```
┌──────────────────────────────────────────────────┐
│           BreakPointTaskRunner                   │
│  · 加载 task_flow.json                           │
│  · for 步骤 in 流程                              │
│  · while retry < max_retry:                      │
│       执行单步 → 成功: 写断点+1                  │
│       失败: retry++、sleep、记日志               │
│  · 全部成功 → finish                             │
│  · 多次失败 → error（断点停在当前步骤）          │
└──────────────┬─────────────────┬─────────────────┘
               │                 │
    ┌──────────▼──────────┐     ┌▼─────────────────┐
    │  GuiAutomation      │     │  CdpBrowserCtrl  │
    │  pyautogui +        │     │  Chrome DevTools │
    │  pyperclip          │     │  Protocol        │
    └─────────────────────┘     └──────────────────┘
```

## 📦 目录结构

```
autopilot-composer/
├── SKILL.md                      # 技能文档（SkillHub 用）
├── README.md                     # 本文件
├── LICENSE
├── .gitignore
└── scripts/
    ├── main_task.py              # 任务编排入口
    ├── cdp_engine.py            # 浏览器 CDP 引擎
    ├── gui_engine.py            # 桌面 GUI 引擎
    ├── config.json              # 全局配置
    ├── breakpoint.json          # 断点状态（运行时生成）
    ├── requirements.txt         # Python 依赖
    ├── demo.html                # 本地测试页
    ├── task_flow.example.json   # 任务流程示例
    └── run_silent.bat           # 后台静默运行脚本（本地版）
```

## 🚀 快速开始

### 1. 安装依赖

```bash
cd scripts
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 启动 Chrome 调试端口（关键）

Chrome 111+ 必须加 `--remote-allow-origins=*`，否则 WebSocket 握手会被 403 拒绝。

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="C:\temp\apc_profile" ^
  --no-first-run --no-default-browser-check --new-window
```

验证：浏览器访问 `http://127.0.0.1:9222/json/version` 能看到版本信息即就绪。

### 3. 配置任务流程 `task_flow.json`

优先读取 `config.json` 中 `task_flow_path` 指定的文件；不存在则回退到内置占位 demo。

```json
[
  { "type": "browser", "func": "open_url",   "args": ["https://www.bing.com"] },
  { "type": "browser", "func": "input_text", "args": ["#sb_form_q", "AutoPilot Composer"] },
  { "type": "browser", "func": "click_elem", "args": ["#sb_form_go"] },
  { "type": "browser", "func": "screenshot", "args": ["./bing_result.png"] }
]
```

### 4. 运行

```bash
cd scripts
python main_task.py
```

窗口说明：`gui` 引擎方法（open_software / click_icon / input_text / drag_move / hot_key）走桌面自动化；`browser` 引擎方法（open_url / input_text / click_elem / screenshot）走 CDP。详见 `SKILL.md`。

## 🧪 本地真实示例（已验证）

项目自带 `demo.html` 与 `task_flow.example.json`：

- 打开本地测试页 → 输入框写入文本 → 点击按钮 → 截图

在真实 Chrome 151 + 真实桌面（1600×900）下完整跑通，截图中可看到输入框文本与「已点击，输入内容：…」状态，证明 CDP 引擎在真实浏览器中工作正常。

## ⚠️ 安全提示

- 该技能会**真实控制鼠标、键盘、浏览器**，请仅在受控环境或测试账号中运行。
- 不要对他人账号、生产系统做未经允许的操作。
- GUI 操作前请保存好当前工作，避免误点。

## 📄 许可证

MIT © csc7878. 详见 [LICENSE](./LICENSE)。

## 🔗 相关

- SkillHub 技能页：https://skillhub.cn/skills/user_36623aa7/autopilot-composer
