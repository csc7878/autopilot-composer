# AutoPilot Composer (v3.6.1)

> 桌面 GUI（pyautogui）+ 浏览器 CDP 双引擎 RPA 框架：把「打开软件 → 点点鼠标 → 浏览器填表 → 截图留痕」的重复流程写成一份清单自动跑，**不会写代码也能用**。

把重复流程写进 `task_flow.json` 一键回放；或者更省事——**让它"看一遍"你的操作，它自动学会并生成清单**。中途崩了能从断点继续，关键步骤自带结果校验。

## ✨ 特性

| 能力 | 说明 | 版本 |
|---|---|---|
| **双引擎** | 桌面 `pyautogui` 引擎 + 浏览器 `CDP` 引擎，覆盖 GUI 与网页混合流程 | 1.0 |
| **断点续跑** | 出错停在断点，修复后从当前步骤继续，不重头跑（`--reset` 可清空重来） | 1.0 |
| **自动重试** | 单步失败按 `max_retry` 重试，超过则暂停并写错误断点 | 1.0 |
| **录制成流程** | 网页录制器（`recorder.py`）+ 桌面录制器（`desktop_recorder.py`）+ 合并会话（`record_session.py`） | 3.3 |
| **本地元素库** | 自动记忆稳定选择器（placeholder / aria-label / type 优先），连续出错自动弃用、恢复后自动复活 | 3.3 / 3.4.1 |
| **T1 直连层** | 跳过点界面，直接调 API / 跑 CLI / 查 SQL；凭证走 Windows 凭据管理器，SQL 四重安全 + 审计日志 | 3.4 |
| **结果校验（检查点）** | 关键步骤可声明"干完自己核对"，失败即报错而非静默往下跑 | 3.4.1 |
| **置信度评分** | 每步打 0~5 分"把握分"，低分可主动停下询问；识别登录页跳转 / 错误页 / 弹窗等环境陷阱 | 3.5 |
| **录制时自动生成检查点** | 录制中一边看你操作，一边把"页面跳到哪了 / 输入是否真填进去 / 有没有保存成功"写成 `verify` 断言；输出 `verify_candidates.md` 逐条说明加与未加的原因 | 3.6 |
| **四层降级** | T1 直连 → T2 CDP → T3 UIA → T4 坐标兜底，逐级回退 | 3.2 |
| **后台静默** | 支持 `pythonw` / `run_silent.bat` 无窗口运行 | 1.0 |

## 🧱 架构

```
┌──────────────────────────────────────────────────────────┐
│                  BreakPointTaskRunner                    │
│  · 加载 task_flow.json                                   │
│  · for 步骤 in 流程:                                     │
│      Tier 解析 → 执行 → verify 校验 → 记置信度 / 战绩    │
│      失败: retry++ (≤ max_retry) → 写入断点暂停          │
│  · 支持 --reset 清空断点 / --health 元素库体检           │
└───────┬─────────────────┬─────────────────┬──────────────┘
        │                 │                 │
 ┌──────▼──────┐  ┌───────▼────────┐  ┌─────▼──────────────┐
 │ T1 直连层   │  │ CdpBrowserCtrl │  │ GuiAutomation      │
 │ API / CLI   │  │ Chrome DevTools│  │ pyautogui +        │
 │ SQL / 凭据  │  │ Protocol       │  │ pyperclip          │
 └─────────────┘  └────────────────┘  └────────────────────┘
```

## 📦 目录结构

```
autopilot-composer/
├── SKILL.md                          # 技能文档（SkillHub 用，含完整 API 与排障）
├── README.md                         # 本文件
├── 小白使用说明.md                    # 零基础图文教程（推荐先看这个）
├── LICENSE / .gitignore
├── docs/
│   ├── t1-direct-layer.md            # T1 直连层（API / CLI / SQL）
│   ├── verify-and-health.md          # 检查点与元素库体检
│   ├── confidence-and-intervention.md# 置信度与主动介入
│   └── recorder-verify-candidates.md # v3.6 录制时自动生成检查点
└── scripts/
    ├── main_task.py                  # 任务编排入口（含 verify / 置信度 / 断点）
    ├── cdp_engine.py                 # 浏览器 CDP 引擎
    ├── gui_engine.py                 # 桌面 GUI 引擎
    ├── browser_launcher.py           # 一键启动调试端口 Chrome
    ├── recorder.py                   # 网页录制器（含结果探测）
    ├── desktop_recorder.py           # 桌面录制器
    ├── record_session.py             # 网页 + 桌面合并录制会话
    ├── chat_mode.py                  # 对话式录制
    ├── confirm_box.py                # 人工确认弹窗
    ├── check_probe_live.py           # 录制探测自检（真机验证用）
    ├── core/                         # 核心模块
    │   ├── verify.py                 #   结果校验执行器
    │   ├── verify_advisor.py         #   ★v3.6 检查点自动推断
    │   ├── confidence.py             #   置信度评分
    │   ├── element_repo.py           #   元素库（自动体检）
    │   ├── tier_resolver.py          #   四层降级解析
    │   ├── local_http.py             #   ★v3.6 本机回环绕过代理
    │   ├── env_guard.py / observer.py / checkpoint.py / step_stats.py
    │   ├── api_client.py / api_registry.py / credential_manager.py
    │   ├── cli_executor.py / cli_registry.py
    │   └── db_client.py / db_registry.py / db_security.py
    ├── config.json                   # 全局配置
    ├── requirements.txt              # Python 依赖
    ├── demo.html / demo2.html        # 本地测试页
    ├── probe_demo.html               # 录制探测自检页
    ├── task_flow.example.json        # 任务流程示例
    ├── e2e_v3*_test.py               # 离线回归测试（无需浏览器）
    └── run_silent.bat                # 后台静默运行（本地版）
```

> 运行时产物（`task_flow.json` / `SOP.md` / `elements.json` / `breakpoint.json` / `step_stats.json` / `checkpoints.json` / `verify_candidates.md` / `run_log.log` / 录制回放脚本）按 `.gitignore` 不入库、不打进技能包。

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
python browser_launcher.py            # 推荐：自动带齐参数
```

或手动：

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="C:\temp\apc_profile" ^
  --no-first-run --no-default-browser-check --new-window
```

验证：浏览器访问 `http://127.0.0.1:9222/json/version` 能看到版本信息即就绪。

> ⚠️ **代理软件用户注意**：若装有 Clash / v2ray 一类代理，旧版会出现"Chrome 明明开着却报 502"。
> v3.6.0 起**连本机浏览器**的通道（`core/local_http.py`）已自动绕过代理；只有对外调用的 T1 直连仍走代理。

### 3. 配置任务流程 `task_flow.json`

优先读取 `config.json` 中 `task_flow_path` 指定的文件；不存在则回退到内置占位 demo。

```json
[
  { "type": "browser", "func": "open_url",   "args": ["https://www.bing.com"] },
  { "type": "browser", "func": "input_text", "args": ["#sb_form_q", "AutoPilot Composer"] },
  { "type": "browser", "func": "click_elem", "args": ["#sb_form_go"],
    "verify": [{ "type": "url_contains", "arg": "/search" }] },
  { "type": "browser", "func": "screenshot", "args": ["./bing_result.png"] }
]
```

### 4. 运行

```bash
python main_task.py                  # 从断点续跑（无断点则从头）
python main_task.py --reset          # 清空断点从头跑
python main_task.py --health         # 元素库体检
```

### 5. 或者：先录一遍，让它自己学

```bash
python recorder.py --verify auto     # 网页录制（默认 auto，自动生成检查点）
python desktop_recorder.py           # 桌面录制（默认 all）
python record_session.py             # 网页 + 桌面同时录，合并成一份流程
```

录完会得到 `task_flow.json` + `SOP.md`（人工可读步骤说明）+ **`verify_candidates.md`**（本次自动加了哪些检查点、为什么加、哪些跳过、为什么跳过）。

窗口说明：`gui` 引擎方法（`open_software` / `click_icon` / `input_text` / `drag_move` / `hot_key`）走桌面自动化；`browser` 引擎方法（`open_url` / `input_text` / `click_elem` / `screenshot` / `drag` / `hover` / `key_press` / `upload_file`）走 CDP。完整动作清单见 `SKILL.md`。

## 🧪 本地真实示例（已验证）

项目自带 `demo.html` / `demo2.html` / `probe_demo.html` 与 `task_flow.example.json`：

- `demo.html`：打开本地测试页 → 输入框写文本 → 点按钮 → 截图。已在真实 Chrome 151 + 真实桌面（1600×900）完整跑通，截图可看到输入内容与「已点击，输入内容：…」状态。
- `probe_demo.html` + `check_probe_live.py`：真机自检录制探测链路（点一下看它是否真的捕捉到"变化前 / 变化后"）。需先开调试端口 Chrome。

### 离线回归测试（无需浏览器 / 无需联网）

```bash
python e2e_v341_test.py        # 检查点与元素库体检
python e2e_v350_test.py        # 置信度与主动介入
python e2e_v360_test.py        # v3.6 检查点自动推断 + 回环绕代理（122 项断言）
python e2e_v360_js_test.py     # 驱动 recorder_probe_js_test.js（47 项断言，DOM 桩跑真实注入脚本）
```

合计 275 项断言全部通过。

## ⚠️ 安全提示

- 该技能会**真实控制鼠标、键盘、浏览器**，请仅在受控环境或测试账号中运行。
- 不要对他人账号、生产系统做未经允许的操作；跑之前先保存手头工作。
- 桌面图标识别靠像素比对，换显示器或改缩放比例后可能失效。
- 自动生成的检查点**只断言"亲眼观察到的事实"**；密码 / 验证码 / 身份证 / 银行卡等敏感字段**永不回读**，不会写进流程文件。
- 录制的截图落在本地 `scripts/recordings/`，注意别误存敏感信息。

## 📄 许可证

MIT © csc7878. 详见 [LICENSE](./LICENSE)。

## 🔗 相关

- SkillHub 技能页：https://skillhub.cn/skills/user_36623aa7/autopilot-composer
- 零基础教程：[小白使用说明.md](./小白使用说明.md)
