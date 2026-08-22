---
name: AutoPilot Composer
displayName: AutoPilot Composer
slug: autopilot-composer
version: 3.2.0
runtime: python
tags:
  - automation
  - cdp
  - pyautogui
  - rpa
  - desktop
  - pipeline
  - record
  - element-repository
  - components
  - browser-launcher
  - preset-elements
description: 桌面 GUI（pyautogui）+ 浏览器 CDP 双引擎 RPA，支持「录制→元素库→回放」原子动作建模、复用组件库、操作日志审计与流程挖掘、断点续跑与自动重试，对标影刀/UiPath 的企业级自动化能力。
entry: ./scripts/main_task.py
trigger:
  - 启动长任务自动化
  - 继续断点执行任务
  - 暂停自动化任务
  - 查看运行日志
---

# AutoPilot Composer 使用指南

> 一句话：把“打开软件 → 点点鼠标 → 浏览器填表 → 截图留痕”这样的重复流程写进 `task_flow.json`，一键自动跑；中途崩了也能从断点继续，不用从头再来。

## 1. 适用场景

- 桌面 + 浏览器混合的长流程自动化（如：打开 ERP → 导出 Excel → 打开网页 → 填报 → 截图归档）
- 批量网页填报、查询、数据抓取
- 需要“断点续跑”的关键业务脚本（流程长、环节多，中间出错不能重来）
- 夜间/后台静默跑批任务

## 2. 架构图

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

## 2.1 升级版架构（v3.2.0 新增：元素库 + 组件 + 操作日志）

```
┌──────────────────────────────────────────────────────────────┐
│                      录制 (recorder.py)                       │
│  监听页面操作 → 原始事件流 → Observer.events_to_actions()      │
│        │                            │                          │
│        ▼                            ▼                          │
│   task_flow.json (原子动作)      elements.json (元素库)        │
│   {verb,element_ref,params}      {多策略定位器电池}           │
└───────────────┬──────────────────────┬───────────────────────┘
                │                      │
                ▼                      ▼
┌──────────────────────────────────────────────────────────────┐
│                   回放 (main_task.py)                         │
│  for step:                                                   │
│     element_ref → repo.best_locator → 最稳选择器             │
│     CLI 代码动作 → run_python / run_bash（编码即动作）        │
│     component → 复用参数化组件（Python/JS/子流程）            │
│     OperationLog 记录每步结果（审计 + 流程挖掘 XES）         │
└──────────────────────────────────────────────────────────────┘
```

核心升级点（对应 GUI/CLI/RPA 融合范式，详见 `GUI_CLI_RPA_融合可行性论证.md`）：
- **原子动作建模**：录制不再存脆弱的链式选择器，而是 `{动词, 目标元素(element_ref), 参数}`。
- **元素库 elements.json**：每个元素存「多策略定位器电池」（id>name>placeholder>role>...），回放按稳定性自动回退，改页面一处全局生效。
- **复用组件 components/**：把高频小动作固化成参数化脚本（Python/JS/子流程），下次直接引用。
- **操作日志 operation_log.json**：结构化记录每步执行结果，可导出 XES 直接喂流程挖掘工具（ProM/PM4Py）。

## 3. 前置条件

- **Python**：3.10 及以上（推荐 3.11/3.12）
- **操作系统**：Windows（`gui_engine.py` 使用 `os.startfile` + `pyautogui`，目前面向 Windows 桌面）
- **Chrome**：已安装；需要开启远程调试端口
- **权限**：脚本会真实控制鼠标、键盘、浏览器，请在受控环境或测试账号中使用

## 4. 安装依赖

```bash
cd scripts
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 包含：

- `pyautogui`：桌面鼠标键盘控制
- `pyperclip`：剪贴板输入
- `websocket-client`：连接 Chrome DevTools Protocol
- `pillow`：图像处理
- `opencv-python`：图像识别（`click_icon` 用）

## 5. 启动 Chrome 调试端口（关键）

AutoPilot Composer 通过 CDP 控制 Chrome，因此必须先启动 Chrome 并开放 9222 端口。**Chrome 111 之后必须加 `--remote-allow-origins=*`**，否则 WebSocket 握手会被 403 拒绝。

### 5.1 使用独立临时 profile（推荐，不影响你日常使用）

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="C:\temp\apc_profile" ^
  --no-first-run ^
  --no-default-browser-check ^
  --new-window
```

### 5.2 在已打开的 Chrome 上附加调试（不推荐，可能影响当前标签）

如果 Chrome 已经在运行，直接加端口通常需要重启；最简单的方式还是上面的独立 profile。

### 5.3 验证端口已开启

打开浏览器访问 `http://127.0.0.1:9222/json/version`，如果能看到 Chrome 版本信息，说明 CDP 服务已就绪。

## 6. 配置任务流程 `task_flow.json`

`main_task.py` 优先读取 `config.json` 中 `task_flow_path` 指定的文件；若不存在，则回退到内置占位 demo。

### 6.1 文件位置

默认与 `main_task.py` 同目录，文件名 `task_flow.json`（可通过 `config.json` 修改）。

### 6.2 单步格式

```json
{
  "type": "browser|gui",
  "func": "方法名",
  "args": ["参数1", "参数2"]
}
```

| 引擎 | `type` | 方法 | 示例 args | 说明 |
|------|--------|------|-----------|------|
| CDP 浏览器 | `browser` | `open_url` | `["https://example.com"]` | 在已连接的标签页中导航到指定 URL |
| CDP 浏览器 | `browser` | `input_text` | `["#username", "myname"]` | 向 CSS 选择器匹配的输入框写入文本 |
| CDP 浏览器 | `browser` | `click_elem` | `["#submit"]` | 点击指定元素 |
| CDP 浏览器 | `browser` | `screenshot` | `["./shot_01.png"]` | 截图并保存为 PNG |
| 桌面 GUI | `gui` | `open_software` | `["C:\\Windows\\notepad.exe"]` | 启动软件 |
| 桌面 GUI | `gui` | `click_icon` | `["btn_ok.png", 0.8]` | 在屏幕上识别图标并点击（confidence 默认 0.8） |
| 桌面 GUI | `gui` | `input_text` | `["你好世界"]` | 通过剪贴板粘贴文本 |
| 桌面 GUI | `gui` | `drag_move` | `[100, 200, 300, 400]` | 从 (x1,y1) 拖拽到 (x2,y2) |
| 桌面 GUI | `gui` | `hot_key` | `["ctrl", "s"]` | 组合键 |
| CDP 浏览器 | `browser` | `drag` | `["#from", "#to"]` | 从一个元素拖拽到另一个元素（坐标级拖拽） |
| CDP 浏览器 | `browser` | `hover` | `["#menu"]` | 悬停在元素上 |
| CDP 浏览器 | `browser` | `key_press` | `[["Control", "c"]]` | 按键 / 组合键（参数为按键 token 列表） |
| CDP 浏览器 | `browser` | `upload_file` | `["input[type=file]", ["a.xlsx"]]` | 向文件输入框设置文件（需回放端提供磁盘路径） |
| 桌面 GUI | `gui` | `click_at` | `[500, 400]` | 按屏幕坐标点击（桌面录制产出） |
| 桌面 GUI | `gui` | `double_click_at` | `[600, 300]` | 双击坐标 |
| 桌面 GUI | `gui` | `right_click_at` | `[600, 300]` | 右键坐标 |
| 桌面 GUI | `gui` | `hover_at` | `[600, 300]` | 移动鼠标到坐标 |
| 桌面 GUI | `gui` | `press_keys` | `[["ctrl", "c"]]` | 按键 / 组合键（列表，桌面录制产出） |
| 桌面 GUI | `gui` | `open_software_by_exe` | `["C:\\wx\\WXWork.exe"]` | 按 exe 路径启动软件 |

### 6.3 完整示例

```json
[
  {
    "type": "browser",
    "func": "open_url",
    "args": ["https://www.bing.com"]
  },
  {
    "type": "browser",
    "func": "input_text",
    "args": ["#sb_form_q", "AutoPilot Composer"]
  },
  {
    "type": "browser",
    "func": "click_elem",
    "args": ["#sb_form_go"]
  },
  {
    "type": "browser",
    "func": "screenshot",
    "args": ["./bing_search_result.png"]
  }
]
```

> 提示：CDP 引擎会附加到 Chrome 当前已打开的第一个普通页面标签；`open_url` 会在该标签页中导航，不会新建标签页（受 Chrome 安全限制，`/json/new` 建标签页在部分 Chrome 版本下被禁用）。

## 6.5 录制模式：自动生成 `task_flow.json`（SOP 捕捉）

不想手写流程？AutoPilot Composer 自带**录制器**（`scripts/recorder.py`）：你在调试模式的 Chrome 里正常操作网页，它实时捕获每一次点击 / 输入 / 跳转，停止后一键导出两份可复用文件。

- `task_flow.json` —— AutoPilot Composer **原生播放格式**，可直接用 `main_task.py` 运行（自动获得断点续跑、重试、后台运行）。
- `recorded_flow.js` —— **独立 Playwright 脚本**，用 `node` 直接跑，不依赖本技能（适合给别人或 CI 用）。

> 这就是"网页自动化捕捉 SOP"：录一次 → 导出 → 后续反复播放、改参数、加分支，持续复用与修订。

### 6.5.1 前置条件

与运行一致：Chrome 必须用调试模式启动并开启 9222 端口（见 §5），且至少已打开一个标签页。

### 6.5.2 用法

```bash
cd scripts
.venv\Scripts\activate

# 交互录制：回车开始，在浏览器里操作，回到命令行输入任意内容回车停止
python recorder.py

# 也可指定输出文件名
python recorder.py --out my_flow.json --js my_flow.js
```

### 6.5.3 录制后会得到什么（网页录制器 v2）

- 每次**点击** → `click_elem`（用自动生成的最稳定 CSS 选择器，优先 `#id` / `[name=...]`，否则 `body > ... :nth-child(...)`）。
- 每次**输入**（输入框失焦时的最终值）→ `input_text`，对应 `change` 事件。
- 每次**拖拽**（按住移动超过阈值）→ `drag`（从元素拖到元素）。
- 每次**悬停**（指针在同一元素停留 > 600ms）→ `hover`。
- **键盘 / 组合键**（Enter、Tab、方向键、F 键，以及 Ctrl/Cmd/Alt+ 组合）→ `key_press`。普通可见字符不单独记录（已由 `input_text` 捕获，避免重复）。
- **文件上传**（选中 `<input type=file>` 的文件名）→ `upload_file`。注意浏览器安全限制只能拿到文件名，回放端需提供文件在磁盘上的完整路径。
- 每次**页面跳转**：
  - 进入录制时的首页面 → `open_url`；
  - 由点击 / 按键触发的跳转 → **自动去重**（播放时 `click_elem` / `key_press` 本身就会导航，不再重复 `open_url`）。
- **跨域 iframe**：录制脚本通过 `Page.addScriptToEvaluateOnNewDocument` 注入到所有 frame（含跨域），iframe 内的点击 / 输入 / 拖拽同样会被捕获（选择器相对于该 frame 文档）。

### 6.5.4 复用与修订

- 改流程：直接编辑导出的 `task_flow.json`（增删步骤、改选择器/文本），再 `python main_task.py` 播放。
- 独立运行 JS：`npm i playwright && npx playwright install chromium`，然后 `node recorded_flow.js`。
- 修订建议：录制出的选择器若含 `:nth-child`，可手动替换为更稳定的 `#id` 或业务语义选择器，提升抗页面改版能力。

### 6.5.5 桌面应用录制器（原生 Windows 软件）

除了网页，AutoPilot Composer 还能录制**本机任意 Windows 软件**的操作——企业微信、微信、钉钉、WPS（文字/表格/演示）、金蝶、用友，以及 Excel/Word 等。录制器通过 `pynput` 全局监听鼠标键盘，并用 `win32gui`+`psutil` 识别当前前台窗口（按 exe 标注应用名），把操作转成可回放的 `gui` 类型步骤。

```bash
cd scripts
.venv\Scripts\activate
python desktop_recorder.py            # 回车开始，操作各类软件，回车停止
python desktop_recorder.py --out desktop_flow.json --py recorded_desktop.py
```

导出两份：

- `desktop_flow.json` —— AutoPilot Composer 原生 `gui` 流程，可并入主 `task_flow.json` 播放。
- `recorded_desktop.py` —— 独立 pyautogui 脚本，直接 `python recorded_desktop.py` 运行。

捕获的动作：点击 / 双击 / 右键、拖拽、悬停、连续输入文本（自动聚合成一段 `input_text`）、组合键（如 Ctrl+C）、以及应用切换（`open_software` 记录 exe 路径）。详见 `scripts/desktop_recorder_README.md`。

> 说明：原生应用没有网页那样的 CSS 选择器，桌面录制以**屏幕坐标 + 前台窗口 exe** 为定位依据。回放时请保持相同分辨率 / 窗口位置；若窗口位置会变，建议录制后把 `click_at` 坐标改为基于窗口相对坐标或图像识别（可结合 `click_icon`）。

### 6.5.6 合并录制会话（网页 + 桌面 一起录）

最常见的真实场景是「在软件里复制 → 切到网页填报」「在 WPS 改表 → 在金蝶录数」。合并会话让你**同时**录制网页和桌面，按真实时间顺序交错合并，一次导出 4 份产物：

```bash
cd scripts
.venv\Scripts\activate
python record_session.py                  # 网页+桌面同时录
python record_session.py --desktop-only   # 只录桌面
python record_session.py --web-only      # 只录网页
```

导出：

- `task_flow.json` —— **统一流程**（browser / gui 混排，按时间排序），`main_task.py` 直接播放。
- `recorded_flow.js` —— 网页部分独立 Playwright 脚本。
- `recorded_desktop.py` —— 桌面部分独立 pyautogui 脚本。
- `SOP.md` —— 人读版操作手册，每步标注所属应用（如【企业微信】【WPS 文字】【网页】）。

这样你就拥有了一条从「手动操作」到「可复用 SOP」的完整链路：录一次 → 导出 → 反复播放 / 改参数 / 加分支，持续修订。

## 7. 运行

```bash
cd scripts
python main_task.py
```

运行产物：

- `run_log.log`：详细执行日志
- `breakpoint.json`：断点状态，出错时会停在当前步骤
- 截图文件：按 `task_flow.json` 中 `screenshot` 步骤生成

## 8. 断点续跑与重试机制

### 8.1 自动重试

`config.json` 中 `max_retry` 控制单步最大重试次数。某一步失败后，会等 2 秒再试；超过次数则暂停，并把 `breakpoint.json` 设为 `error`。

```json
{
  "max_retry": 3
}
```

### 8.2 断点续跑

出错后，修复问题，再运行 `main_task.py`，它会读取 `breakpoint.json` 中的 `current_step`，从该步骤继续执行，不会从头开始。

如果你希望强制从头执行，可删除 `breakpoint.json` 或把 `current_step` 改为 0。

### 8.3 状态说明

| task_status | 含义 |
|-------------|------|
| `stop` | 尚未启动或已重置 |
| `running` | 执行中，每成功一步 current_step+1 |
| `error` | 某步多次重试失败，停在 current_step |
| `finish` | 全部步骤执行完成 |

## 9. 配置项 `config.json`

```json
{
  "task_name": "long_flow_auto_task",
  "max_retry": 3,
  "delay_base": 1.2,
  "log_level": "INFO",
  "save_breakpoint_path": "./breakpoint.json",
  "log_save_path": "./run_log.log",
  "snapshot_err_path": "./error_img/",
  "run_mode": "background",
  "browser_cdp_port": 9222,
  "gui_safe_mode": true,
  "task_flow_path": "./task_flow.json"
}
```

| 字段 | 说明 |
|------|------|
| `max_retry` | 单步失败最大重试次数 |
| `delay_base` | 每步成功后等待秒数 |
| `browser_cdp_port` | Chrome CDP 端口 |
| `task_flow_path` | 任务流程文件路径 |

## 10. 后台/静默运行

### 10.1 使用桌面本地版的 `run_silent.bat`

如果你使用的是 `autopilot-composer-3.1.0(本地版)`，直接双击 `run_silent.bat`，任务会在后台运行，不占用当前命令行窗口。

### 10.2 自建后台脚本

```bash
start /min pythonw scripts\main_task.py
```

`pythonw` 是无窗口模式，适合 Windows 后台跑批。

## 11. 常见问题与排错

### 11.1 WebSocket 403 Forbidden

错误信息：

```text
Rejected an incoming WebSocket connection from the http://127.0.0.1:9222 origin.
Use the command line flag --remote-allow-origins=* ...
```

**解决**：启动 Chrome 时加上 `--remote-allow-origins=*`。

### 11.2 WebSocket 404 Not Found（/devtools/browser）

AutoPilot Composer 已改用 **page 级 WebSocket 通道**（通过 `http://127.0.0.1:9222/json` 发现可用标签页并直连），不再依赖 `/devtools/browser`。如果仍遇到 404，请确认：

- Chrome 已用 `--remote-debugging-port=9222` 启动
- 至少有一个普通页面标签页存在（`about:blank` 也可以）

### 11.3 `input_text` / `click_elem` 报 NOT_FOUND

- 检查 CSS 选择器是否正确
- 检查页面是否已加载完成（CDP 引擎已内置 readyState 等待，但复杂 SPA 可能需要额外延时）
- **iframe 内元素**：录制器 v2 已能捕获 iframe（含跨域）内的操作，但播放端 `cdp_engine` 默认在当前主文档 `document` 上执行 `querySelector`。若录制到的选择器来自 iframe 内部，回放前需确认该 iframe 已存在，必要时在 `task_flow.json` 中改为先 `open_url` 直接打开该 iframe 的 src，或后续版本会增加 frame 上下文切换。

### 11.4 `click_icon` 找不到图

- 截图必须与原图显示大小、分辨率一致（pyautogui 用像素级匹配）
- 可尝试降低 `confidence`（但不建议低于 0.7，容易误点）
- 确保当前显示器分辨率与截图时一致

### 11.5 流程跑完不想重头再来

保留 `breakpoint.json` 即可断点续跑；想重置则删除该文件或把 `current_step` 改为 0。

## 12. 本地真实示例（已验证）

项目提供了一个本地 demo 页和对应流程，用于验证真实 Chrome 环境：

- `demo.html`：一个带输入框和按钮的测试页
- `task_flow.json`：打开该页面 → 输入文本 → 点击按钮 → 截图

效果：截图中可看到输入框文本和“已点击，输入内容：…”状态文本，证明浏览器 CDP 引擎在真实 Chrome 中成功运行。

## 13. 安全与合规提示

- 该技能会真实控制鼠标、键盘、浏览器，请仅在受控环境或测试账号中运行。
- 不要对他人账号、生产系统做未经允许的操作。
- GUI 操作前请保存好当前工作，避免误点。

## 14. 自带浏览器启动器（零下载、零体积）

不必再手动敲一长串调试参数。运行 `browser_launcher.py` 即可一键拉起本机已安装的
Chrome（开源 Chromium 的商业构建），并开启远程调试端口：

```bash
python scripts/browser_launcher.py --port 9222 --open https://www.baidu.com
```

- 自动查找系统 Chrome / Chromium（Windows / macOS / Linux）；
- 若端口已有可用调试实例，直接复用，不重复拉起；
- 每次拉起使用独立 user-data-dir，互不污染日常浏览器；
- 返回 `{new, port, executable, profile}`，可被录制器 / 回放器复用。

> 选择「复用系统 Chrome」而非额外下载 Chromium：Chrome 本身就是开源 Chromium
> 的官方构建，体积零增加、维护零成本，且 CDP 协议完全互通。

## 15. 预置元素库（提前定义，轨迹快速定位）

`preset_elements.json` 针对常见站点（百度 / Google / GitHub / Bing 及通用登录框）
预置了多策略定位器。录制时若元素命中预置（同 domain，或 id 精确匹配），动作直接引用
`preset_*` 作为 `element_ref`，不再重复登记；回放时预置库与用户元素库合并，预置兜底命中。

这让「提前把元素库定义好 → 用户录轨迹 → 自动定位」的体验更顺：
- 用户录制百度搜索：`#kw` 自动命中 `preset_baidu_search_input`，即便页面改版也能靠
  name/placeholder 回退定位；
- 通用 `#username` / `#password` / 提交按钮也内置了兜底定位。

## 15. 对话驱动 + 实时确认录制

把技能从「命令行脚本」升级为「对话即可用」。用户无需记忆命令，直接用自然语言驱动：

```bash
python scripts/chat_mode.py "打开 https://www.baidu.com"   # 拉起自带浏览器并导航
python scripts/chat_mode.py "开始录制"                      # 开启带 GUI 弹窗确认的交互录制
python scripts/chat_mode.py "停止"                          # 结束录制并落盘
python scripts/chat_mode.py "回放"                          # 回放最近一次录制
```

**意图解析**（`chat_mode.parse_intent`，离线正则/关键词，可嵌入专家外壳）：
- `打开/访问/goto` + URL → `open`：调用 `browser_launcher.launch` 拉起（或复用）自带浏览器并导航。
- `开始录制/record` → `record`：启动 `InteractiveRecorder`，自动按域名归档到 `recordings/<host>/`。
- `停止/结束` → `stop`：结束录制，SOP 与元素库已实时落盘。
- `回放/播放/run` → `replay`：用 `main_task` 回放最近（或指定）录制产物。

**实时确认弹窗**（`confirm_box.py`，基于 tkinter）：
- 录制过程中，**每一步操作都会弹出系统级窗口**（标题 + 动作摘要 + 「记录 ✓ / 跳过 ✗」按钮）。
- 用户确认后才把这一步写入 `task_flow.json`（SOP）与 `elements.json`（元素库）；超时（默认 8s）自动跳过，避免卡死。
- 非 GUI 环境自动降级为命令行 `y/N` 确认。

**实时落盘 + 归档**：产物按域名自动归入 `recordings/<host>/`（如 `recordings/www.baidu.com/`），并与预置元素库合并，回放时优先命中。

## 16. 目录结构

autopilot-composer-3.2.0/
├── SKILL.md                 # 本文档
├── scripts/
│   ├── main_task.py         # 任务编排入口（播放）
│   ├── cdp_engine.py        # 浏览器 CDP 引擎（播放端，多策略定位 / URL 过滤连接标签页）
│   ├── gui_engine.py        # 桌面 GUI 引擎（播放端）
│   ├── recorder.py          # 网页录制器 v2（导出 task_flow + 元素库 + Playwright）+ InteractiveRecorder 实时确认
│   ├── browser_launcher.py  # 自带浏览器启动 器（一键拉起 Chrome 调试实例）
│   ├── confirm_box.py       # 系统 GUI 确认弹窗（录制逐步确认）
│   ├── chat_mode.py         # 对话驱动入口（自然语言意图分发）
│   ├── preset_elements.json # 预置元素库（常见站点多策略定位器）
│   ├── desktop_recorder.py  #  * 桌面应用录制器
│   ├── record_session.py    # 合并录制会话
│   ├── demo2.html           # 自带浏览器验证页
│   ├── e2e_preset_test.py   # 端到端验证脚本（自带浏览器+预置库+回放）
│   ├── e2e_chat_test.py     # 端到端验证脚本（对话驱动+弹窗确认+回放）
│   ├── config.json / breakpoint.json / task_flow.json
│   └── requirements.txt


```

## 17. 版本记录

- **3.1.0** 桌面 GUI + 浏览器 CDP 双引擎、断点续跑、自动重试、运行日志、后台静默运行。
- **本次修订（2026-08-20）**：修复 `main_task.py` 缩进与重试循环；`cdp_engine.py` 改为 page 级 CDP 通道，适配 Chrome 111+ 的 `--remote-allow-origins=*` 要求；`task_flow` 支持外部 `task_flow.json` 配置。
- **录制能力扩展（2026-08-22）**：网页录制器 v2 新增拖拽/悬停/键盘组合键/文件上传捕获，并支持跨域 iframe；新增桌面录制器与合并录制会话；播放端同步新增 `drag/hover/key_press/upload_file` 等回放方法。
- **v3.2.0（2026-08-22）**：① 新增 `browser_launcher.py` 自带浏览器启动器；② 新增 `preset_elements.json` 预置元素库；③ 录制器/回放器接入预置库合并；④ 新增 `confirm_box.py` + `chat_mode.py`（对话驱动 + 实时 GUI 弹窗确认录制）；⑤ `cdp_engine.connect` 支持 `url_filter` 精准连接目标标签页；⑥ `recorder.js` 的 bestSelector 修复 html 误判；⑦ 端到端验证通过（对话驱动 → 实时确认 → 落盘 → 回放 PASS）。
