# T1 直连层使用指南（v3.4.0）

> 对应引擎版本 v3.4.0。本文是 T1 直连层（api / cli / sql）的完整使用文档，
> 涵盖 **API 模板注册、凭证管理、CLI/COM/PowerShell、SQL 直连与安全防护、Tier 自动降级**。
> 小白入门请先读《小白使用说明.md》，本文面向进阶用户。

---

## 1. 为什么需要 T1 直连层？

GUI 自动化（点按钮、填表单）有两个天然弱点：

1. **脆**：界面改版、元素改名、加载慢一点，定位器就失效——研究表明约 **73.6%** 的 GUI 测试失败源于脆弱的定位器（Hammoudi & Stevenson, 2016）。
2. **慢**：为了提交一条数据，要打开页面 → 等渲染 → 找元素 → 填 → 点，而背后的真相往往只是一次 HTTP 请求或一条 SQL。

T1 直连层的思路：**能直接"喊话"系统（调 API / 执行命令 / 查数据库），就不去模拟人点界面**。CodeAct（ICML 2024）证明：可执行代码动作相比离散 UI 动作，成功率 +20%、动作数 -30%。

## 2. 四层自动化模型（Tier）

| Tier | 名称 | 通道 | 稳定性 | 速度 |
|------|------|------|--------|------|
| **T1** | api / cli / sql | 直调 API、命令行、数据库 | ★★★★★ 不受界面改版影响 | 最快 |
| **T2** | cdp_element | 浏览器 CDP 元素定位 | ★★★★ | 快 |
| **T3** | uia_element | 桌面 UIA 元素定位 | ★★★ | 中 |
| **T4** | coord | 屏幕坐标兜底 | ★ | 慢（依赖分辨率） |

**回放策略**：`main_task.py` 对每一步先问 TierResolver"有没有 T1 路径？"——

- 有（步骤是 `api/cli/sql`，或 browser/gui 步骤带 `t1_ref`）→ **先试 T1**；
- T1 成功 → 跳过 GUI，直接进入下一步（回放日志显示 `T1(call_api)`）；
- T1 失败（rc≠0 或 HTTP 非 2xx）→ **自动降级**到 T2/T3/T4 走界面路径。

```
✅ 步骤 3/12 [调用API query_orders] {T1(call_api)}      ← 走了直连，没开界面
✅ 步骤 4/12 [点击 提交按钮] {T2(cdp)}                   ← 这步没有 T1，走界面
```

这样即使某天 API 变了，流程也不会死——自动落回界面点击，保证任务跑完。

## 3. 快速开始：三步用上 T1

以"直接调接口查订单列表"为例：

**第 1 步：把 API 登记进注册表**（`scripts/api_registry.json`）：

```json
{
  "version": 1,
  "templates": {
    "query_orders": {
      "method": "GET",
      "base_url": "https://api.example.com",
      "path": "/orders/list",
      "auth": { "type": "bearer" },
      "query_params": { "page": 1, "size": 20 },
      "timeout": 30,
      "max_retry": 2,
      "assertions": [ { "type": "status", "expected": 200 } ]
    }
  }
}
```

**第 2 步：把 Token 存进凭证管理器**（不写进 task_flow.json！）：

```bat
cd scripts
python -c "from core.credential_manager import get_manager; get_manager().store('my_api_token', {'token': 'sk-xxxx'}, backend='keyring')"
```

**第 3 步：在 task_flow.json 里直接调用**：

```json
[
  { "type": "api", "func": "call_api", "args": ["query_orders"],
    "credential_ref": "my_api_token" }
]
```

跑 `python main_task.py`，回放日志会显示这一步走了 `T1(call_api)`——不开浏览器、不找元素，几十毫秒完成。

## 4. API 模板注册（api_registry.json）

### 4.1 模板字段全解

| 字段 | 类型 | 说明 |
|------|------|------|
| `method` | str | GET / POST / PUT / PATCH / DELETE |
| `base_url` | str | 协议+域名，如 `https://api.example.com` |
| `path` | str | 接口路径，如 `/orders/list` |
| `headers` | dict | 默认请求头 |
| `auth` | dict | 鉴权声明：`{"type": "bearer"|"basic"|"api_key"|"cookie"}` |
| `query_params` | dict | 默认 query 参数（GET 时自动附加） |
| `body` | dict/str | 默认请求体（dict 按 JSON 发送） |
| `timeout` | int | 超时秒数，默认 30 |
| `max_retry` | int | 失败重试次数，默认 2（间隔递增 1.5s/3s） |
| `assertions` | list | 响应断言，见 4.3 |

### 4.2 两种登记方式

**方式 A：手动编辑 `scripts/api_registry.json`**（如上例）。

**方式 B：录制时自动捕获（推荐，零手写）**——v3.4.0 起，`recorder.py` / `record_session.py` 录制时会自动开启 CDP Network 监听：

- 你在页面上点"查询订单"，录制器在记下"点击按钮"的同时，捕获背后的 `GET /orders/list` 请求；
- 停止录制后自动生成/合并进 `api_registry.json`（模板名按 URL 自动推断，如 `GET_orders_list`）；
- 并在该 browser 步骤上挂 `t1_ref` 关联（3 秒内配对），回放时自动优先走 API。

静态资源（图片/字体/样式）和常见分析追踪（google-analytics / hotjar / sentry）会被自动过滤，不会污染注册表。

### 4.3 响应断言

| 断言类型 | expected 示例 | 判定 |
|----------|---------------|------|
| `status` | `200` | HTTP 状态码相等 |
| `body_contains` | `"orders"` | 响应体包含该子串 |
| `body_path` | `"data.total"` | JSON 路径取值非空/为真 |
| `rc_zero` | — | 内部执行码为 0 |

断言失败时该步 rc=2，回放器视为 T1 失败 → 自动降级走界面。

### 4.4 运行时覆盖参数

`call_api` 的第 2 个参数可覆盖模板默认值（如换页码、传业务参数）：

```json
{ "type": "api", "func": "call_api",
  "args": ["query_orders", { "query_params": { "page": 2, "size": 50 } }],
  "credential_ref": "my_api_token" }
```

## 5. 凭证管理（credential_manager.py）

**核心原则：密码/Token 永远不进 task_flow.json、不进注册表文件。** 流程清单可能要分享、要入库，凭证必须隔离存储，用 `credential_ref` 名字引用。

### 5.1 三种存储后端

| 后端 | 特点 | 适用 |
|------|------|------|
| `keyring`（默认） | 存入 Windows 凭据管理器，OS 级加密 | 日常本机使用（推荐） |
| `env` | 存环境变量 `APC_CRED_<REF大写>` | CI/CD、容器 |
| `file` | AES-Fernet 加密 JSON（`.credentials.json.enc` + `.key`） | 无 keyring 的环境（Linux 服务器） |

### 5.2 常用操作

```python
from core.credential_manager import get_manager, resolve_credential

mgr = get_manager()

# 存（backend 可选 auto/keyring/env/file，auto=有 keyring 用 keyring）
mgr.store("kingdee_token", {"token": "sk-xxxx"}, backend="keyring")

# 取
cred = mgr.get("kingdee_token")          # -> {"token": "sk-xxxx"}

# 便捷函数：按 ref 解析（找不到时兜底读同名环境变量）
cred = resolve_credential("kingdee_token")

# 删
mgr.delete("kingdee_token")
```

### 5.3 API 凭证的格式

`auth.type` 决定凭证字典里放什么：

| auth.type | 凭证字段 | 注入方式 |
|-----------|----------|----------|
| `bearer` | `{"token": "..."}` | `Authorization: Bearer <token>` |
| `basic` | `{"username": "...", "password": "..."}` | Basic Auth |
| `api_key` | `{"key": "...", "header": "X-Api-Key"}`（header 可省略） | 指定头注入 |
| `cookie` | `{"cookie": "session=..."}` | `Cookie` 请求头 |

### 5.4 SQL 凭证的格式

SQL 连接凭证存**完整连接配置**（密码也在里面，所以必须走 keyring/加密文件）：

```python
mgr.store("crm_db", {
    "db_type": "pymysql",          # pyodbc | pymysql | sqlite3
    "host": "192.168.1.10",
    "port": 3306,
    "database": "crm",
    "username": "reader",
    "password": "******",
    "read_only": True,             # 强烈建议查询场景开
    "sensitive_fields": ["phone", "id_card"]   # 查询结果自动脱敏
}, backend="keyring")
```

> 也可以用 `db_registry.json` 登记连接信息（不含密码，密码用 `credential_ref` 指向凭证管理器），团队共享连接配置、个人各存各的密码时用这种。

### 5.5 安全提醒

- `.credentials.json.enc` 与 `.key` 文件、`api_registry.json` 等运行产物**已配置为不入 git**（见 `.gitignore`），别手动 `git add -f`；
- 分享 task_flow.json 是安全的（里面只有 `credential_ref` 名字，没有密钥本身）；
- 换机器需重新 `store` 一次凭证；离职/换密钥记得 `delete` + 系统侧吊销。

## 6. CLI / COM / PowerShell（cli_executor + cli_registry）

### 6.1 四种执行方式

task_flow 中 `type: "cli"` 步骤支持：

```json
{ "type": "cli", "func": "run_python",  "args": ["print(sum(range(100)))"] }
{ "type": "cli", "func": "run_bash",    "args": ["dir /b C:\\logs"] }
{ "type": "cli", "func": "run_com",     "args": ["Excel.Application", "Workbooks.Open", "C:\\tmp\\a.xlsx"] }
{ "type": "cli", "func": "run_ps",      "args": ["Get-Process | Select-Object -First 5"] }
{ "type": "cli", "func": "run_template","args": ["export_report", "{ 'date': '2026-08-25' }"] }
```

- `run_com`：通过 `win32com` 控制 WPS（`KWPS.Application`）、Excel（`Excel.Application`）、金蝶等提供 COM 接口的软件，**不点界面直接调方法**，需要 `pip install pywin32`；
- `run_template`：**白名单模式**——只执行 `cli_registry.json` 里登记过的模板，任务清单里传参即可，防止流程文件变成任意代码执行器。

### 6.2 注册 CLI 模板（scripts/cli_registry.json）

```json
{
  "version": 1,
  "templates": {
    "export_report": {
      "executor": "subprocess",
      "command": "report-tool.exe export --date {date} --out {out_path}",
      "params": ["date", "out_path"],
      "timeout": 120,
      "cwd": "C:\\tools\\report"
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `executor` | `subprocess` / `com` / `powershell` / `sdk` |
| `command` | 命令模板，`{param}` 占位符运行时替换 |
| `params` | 允许的参数名列表 |
| `timeout` | 超时秒数（默认 120） |
| `cwd` | 工作目录 |

调用时传参：`{"type":"cli","func":"run_template","args":["export_report",{"date":"2026-08-25","out_path":"C:\\tmp\\r.csv"}]}`

## 7. SQL 直连（db_client.py + db_security.py）

### 7.1 三种动作

```json
[
  { "type": "sql", "func": "query", "args": ["SELECT order_no, amount FROM orders WHERE create_date = ?", ["2026-08-25"]],
    "credential_ref": "crm_db" },

  { "type": "sql", "func": "execute", "args": ["INSERT INTO sync_log(order_no, status) VALUES(?, ?)", ["SO-1001", "done"]],
    "credential_ref": "crm_db" },

  { "type": "sql", "func": "transaction",
    "args": [[
      { "sql": "UPDATE orders SET synced = 1 WHERE order_no = ?", "params": ["SO-1001"] },
      { "sql": "INSERT INTO sync_log(order_no, status) VALUES(?, ?)", "params": ["SO-1001", "done"] }
    ]],
    "credential_ref": "crm_db" }
]
```

- `query`：参数化查询，返回 `rows` 列表（敏感字段已脱敏）；
- `execute`：单条写操作，返回 `rows_affected`；
- `transaction`：多条 SQL **原子提交**，任何一条失败整体回滚。

### 7.2 安全防护（四重）

| 防护 | 机制 | 效果 |
|------|------|------|
| **防注入** | 强制参数化（`?` 占位符 + params 数组）+ 危险模式检测（`; DROP TABLE` / `xp_cmdshell` / 注释拼接等正则） | 拼接式 SQL 直接拒绝执行 |
| **只读保护** | 连接配置 `read_only: true` 时，非 SELECT 语句直接抛错 | 误发 DELETE 也伤不到库 |
| **字段脱敏** | `sensitive_fields` 声明的字段自动打码（保留首 2 尾 2 位） | 手机号 `13********88` |
| **审计日志** | 每次 SQL 执行追加 `scripts/db_audit.log`（时间/db/SQL摘要/耗时/影响行数/SQL MD5） | 事后可追溯 |

> 最佳实践：**查询型任务一律 `read_only: true`**；写操作走 `transaction`；别在 SQL 里拼字符串，一律用 `?` 占位符。

### 7.3 驱动依赖

| db_type | 驱动 | 安装 |
|---------|------|------|
| `sqlite3` | Python 内置 | 无需安装（本地文件库/测试用） |
| `pymysql` | PyMySQL | `pip install pymysql`（MySQL/MariaDB） |
| `pyodbc` | pyodbc + ODBC Driver | `pip install pyodbc`，且需装 [Microsoft ODBC Driver 17+ for SQL Server](https://learn.microsoft.com/sql/connect/odbc/)（SQL Server） |

## 8. Tier 降级回放细节（tier_resolver.py）

录制时 Network 捕获自动关联后，browser 步骤长这样：

```json
{
  "type": "browser", "func": "click_elem",
  "args": ["#btn-search"],
  "t1_ref": { "type": "api", "name": "GET_orders_list", "credential_ref": "my_api_token" }
}
```

回放逻辑（`main_task.py` `run_single_step`）：

```
该步有 t1_ref？
 ├─ 是 → 调 API（T1）
 │       ├─ 成功(2xx 且断言过) → 跳过界面点击，进入下一步
 │       └─ 失败 → 降级：照常 CDP 找 #btn-search 点击（T2）
 └─ 否 → 直接走 T2（browser）/ T3（uia）/ T4（coord）
```

- T1 尝试结果会写进运行日志，方便排查"为什么这步走了界面"；
- `t1_ref` 可手工删除（强制走界面）或手工添加（把界面步骤升级为直连）；
- 也可以直接把步骤改写成 `type: "api"` 纯 T1 步骤（不带界面兜底，失败即停，适合强校验场景）。

## 9. 配置参考（scripts/config.json）

```json
{
  "api_registry_path": "api_registry.json",
  "cli_registry_path": "cli_registry.json",
  "db_registry_path": "db_registry.json"
}
```

三个注册表都放在 `scripts/` 目录，首次运行自动创建；路径可改。

## 10. FAQ

**Q1：回放日志里这步显示 T1 但我想要它走界面？**
删掉该步骤的 `t1_ref` 字段，或把 `type: "api"` 改回 `type: "browser"` + 对应 func/args。

**Q2：API 模板自动捕获了一堆我不想要的接口？**
直接编辑 `api_registry.json` 删掉多余模板即可；`t1_ref` 引用了已删除模板时会自动忽略（相当于没有 T1 路径）。

**Q3：`credential_ref` 报"凭证未找到"？**
先 `python -c "from core.credential_manager import get_manager; print(get_manager().get('你的ref'))"` 检查；None 就重新 `store` 一次。注意 keyring 后端在切换 Windows 账户后不可见。

**Q4：SQL 报"read_only 模式禁止写操作"？**
这是保护机制。确认确实要写库后，把凭证里的 `read_only` 改为 `false` 重新 `store`；仅查询任务请保持 `true`。

**Q5：pyodbc 连 SQL Server 报驱动错误？**
安装 Microsoft ODBC Driver 17（或 18）for SQL Server；或在连接配置里把 `driver` 字段改成你机器上已装的名字（控制面板 → ODBC 数据源 → 驱动程序页可查）。

**Q6：COM 报 "pywin32 未安装" / 只支持 Windows？**
`pip install pywin32`；COM 是 Windows 专有机制，跨平台场景请用 subprocess 模板。

---

## 附：T1 模块文件清单

| 文件（scripts/core/） | 职责 |
|------------------------|------|
| `credential_manager.py` | 凭证安全存储（keyring/env/file 三后端） |
| `api_client.py` | HTTP 客户端（全方法 + 4 种鉴权 + 重试 + 断言） |
| `api_registry.py` | API 模板注册表 |
| `db_client.py` | SQL 客户端（三驱动 + 参数化 + 事务） |
| `db_registry.py` | 数据库连接配置注册表（不含密码） |
| `db_security.py` | 注入检测 + 脱敏 + 审计日志 |
| `cli_registry.py` | CLI 命令白名单模板 |
| `cli_executor.py` | subprocess / COM / PowerShell / SDK 执行器 |
| `network_capture.py` | 录制时 CDP Network 域捕获 → 自动生成 API 模板 |
| `tier_resolver.py` | 四层降级解析器 |
