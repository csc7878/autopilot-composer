# AutoPilot Composer 网页录制器（recorder.py · v2）

把"你在网页上的操作"自动变成可复用的自动化脚本。这是 AutoPilot Composer 的 **Record（录制）** 模式，与 `main_task.py` 的 **Play（播放）** 模式配合，形成完整的"录制 → 复用 → 修订"闭环。

## 它能录制什么（v2）

| 你的操作 | 导出为 | 说明 |
|----------|--------|------|
| 进入的首个页面 | `open_url` | 流程起点 |
| 点击链接/按钮 | `click_elem` | 自动选最稳选择器 |
| 在输入框打字（失焦最终值） | `input_text` | 对应 `change` 事件 |
| **拖拽**（按住移动） | `drag` | 从元素拖到元素 |
| **悬停**（停留 > 600ms） | `hover` | 指针稳定停留触发 |
| **键盘 / 组合键**（Enter/Tab/方向键/F键，Ctrl/Cmd/Alt+…） | `key_press` | 普通可见字符不单独记（已由 `input_text` 覆盖） |
| **文件上传**（选中 `<input type=file>`） | `upload_file` | 仅记录文件名，回放需提供完整路径 |
| 点击触发的页面跳转 | （去重） | 播放时 `click_elem` 会自己导航 |
| **iframe 内操作（含跨域）** | 同上 | 通过 `addScriptToEvaluateOnNewDocument` 注入到所有 frame |

## 产物

| 产物 | 用途 | 怎么跑 |
|------|------|--------|
| `task_flow.json` | AutoPilot Composer 原生流程 | `python main_task.py`（断点续跑+重试+后台） |
| `recorded_flow.js` | 独立 Playwright 脚本 | `node recorded_flow.js`（需 `npm i playwright`） |

## 前置条件

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="C:\temp\apc_profile" --new-window
```

至少打开一个标签页并访问目标网站。

## 用法

```bat
cd scripts
.venv\Scripts\activate

python recorder.py                  :: 回车开始，浏览器操作，命令行输入任意内容回车停止
python recorder.py --out tf.json --js flow.js
```

## 选择器策略（从稳到弱）

1. `#id` —— 优先
2. `tag[name="xxx"]` —— 有 name 时用
3. `body > div:nth-child(n) > ...` —— 兜底，建议录制后替换为业务语义选择器

## 复用与修订

- 改流程：编辑 `task_flow.json` 增删步骤、改选择器/文本，再 `python main_task.py`。
- 独立运行：`npm i playwright && npx playwright install chromium` → `node recorded_flow.js`。
- 提稳定性：把 `:nth-child` 换成 `#id` / `[data-testid=...]`。

## 已知限制（v2）

- 文件上传只能记录文件名（浏览器安全限制），回放端必须提供文件在磁盘上的完整路径。
- 录制依赖注入脚本，对启用强 CSP 的站点可能失效。
- iframe 内的操作能被**录制**；但播放端默认在主文档上下文执行，iframe 内部选择器回放前需确保该 frame 已在（见 SKILL.md §11.3）。
- 跨域 iframe 若在录制**开始前**就已存在且从未重新加载，可能漏录；操作时让其重新加载即可被捕获。
