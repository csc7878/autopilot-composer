# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 任务编排入口（播放器）【四层自动化升级版 v3.4.0】

支持六类原子动作：
  - browser   : 浏览器 CDP 引擎（元素库解析 + 多策略定位回退）        T2/T4
  - gui       : 桌面 GUI 引擎（坐标 / UIA）                         T3/T4
  - cli       : 代码执行器（Python / Bash / COM / PowerShell）      T1
  - component : 复用组件（参数化 JS/Py/子流程）
  - api       : HTTP API 直调（T1 直连层，录制时 Network 自动捕获）   T1
  - sql       : SQL 直连数据库（T1 直连层，参数化查询防注入）         T1

四层自动化模型（Tier）：
  T1 api/cli/db   - 直调 API/CLI/SQL（最快最稳，不受 UI 改版影响）
  T2 cdp_element  - 浏览器 CDP 元素定位（稳定，抗改版）
  T3 uia_element  - 桌面 UIA 元素定位（较稳定）
  T4 coord        - 屏幕坐标（最脆弱，兜底）

回放策略：每步先检查是否有 T1 路径，有则先试 T1（直调 API/CLI/SQL），
T1 成功则跳过 GUI 操作；T1 失败自动降级到 T2/T3/T4。
"""
import json
import time
import os
import logging
from datetime import datetime

from gui_engine import GuiAutomation
from cdp_engine import CdpBrowserCtrl
from core.actions import Action
from core.element_repo import ElementRepository
from core.op_log import OperationLog
from core import cli_executor
from core import components as comp_mod
from core.tier_resolver import TierResolver
from core.api_registry import ApiRegistry
from core.cli_registry import CliRegistry
from core.db_registry import DbRegistry


logging.basicConfig(
    filename="./run_log.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)


class BreakPointTaskRunner:
    def __init__(self):
        self.cfg = self.load_config()
        self.break_data = self.load_breakpoint()
        self.gui = GuiAutomation()
        self.browser = CdpBrowserCtrl(self.cfg.get("browser_cdp_port", 9222))
        self.max_retry = self.cfg["max_retry"]
        self.task_flow_path = self.cfg.get("task_flow_path", "./task_flow.json")

        # 元素库 + 操作日志 + 组件目录
        elements_path = self.cfg.get("elements_path", "./elements.json")
        self.repo = ElementRepository.load(elements_path)
        # 合并预置元素库（预置兜底命中，用户元素库覆盖同名 eid）
        preset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "preset_elements.json")
        if os.path.exists(preset_path):
            preset = ElementRepository.load_preset(preset_path)
            self.repo.merge(preset)
        self.oplog = OperationLog(self.cfg.get("oplog_path", "./operation_log.json"))
        comp_dir = self.cfg.get("components_dir",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), "components"))
        comp_mod.set_component_dir(comp_dir)

        # T1 直连层：注册表 + Tier 降级解析器
        self.api_registry = self._load_registry(ApiRegistry, "api_registry_path")
        self.cli_registry = self._load_registry(CliRegistry, "cli_registry_path")
        self.db_registry = self._load_registry(DbRegistry, "db_registry_path")
        self.tier_resolver = TierResolver(
            api_registry=self.api_registry.templates if self.api_registry else {},
            cli_registry=self.cli_registry.templates if self.cli_registry else {},
            db_registry=self.db_registry.connections if self.db_registry else {},
        )

    def load_config(self):
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_registry(self, registry_cls, cfg_key):
        """加载注册表（API/CLI/DB），从 config.json 的路径读或返回空注册表。"""
        path = self.cfg.get(cfg_key)
        if not path:
            # 默认路径：与 main_task.py 同目录
            default_name = {"api_registry_path": "api_registry.json",
                            "cli_registry_path": "cli_registry.json",
                            "db_registry_path": "db_registry.json"}.get(cfg_key)
            if default_name:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), default_name)
        return registry_cls(path) if path else registry_cls()

    def load_breakpoint(self):
        path = self.cfg["save_breakpoint_path"]
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"current_step": 0, "total_step": 100, "task_status": "stop"}

    def save_breakpoint(self, step, status="running", err=""):
        self.break_data["current_step"] = step
        self.break_data["task_status"] = status
        self.break_data["last_run_time"] = str(datetime.now())
        self.break_data["err_msg"] = err
        with open(self.cfg["save_breakpoint_path"], "w", encoding="utf-8") as f:
            json.dump(self.break_data, f, ensure_ascii=False, indent=2)

    def get_task_flow(self):
        return [
            {"type": "gui", "func": "open_software", "args": []},
            {"type": "gui", "func": "click_icon", "args": ["icon1.png"]},
            {"type": "browser", "func": "open_url", "args": ["https://xxx.com"]},
            {"type": "browser", "func": "input_text", "args": ["#user", "123456"]},
            {"type": "gui", "func": "drag_move", "args": [100, 200, 300, 400]},
        ]

    def load_task_flow(self):
        if os.path.exists(self.task_flow_path):
            with open(self.task_flow_path, "r", encoding="utf-8") as f:
                task_list = json.load(f)
            logging.info("已从 %s 加载流程，共 %d 步" % (self.task_flow_path, len(task_list)))
        else:
            task_list = self.get_task_flow()
            logging.warning("未找到 task_flow.json，使用内置占位 demo 流程")
        self.break_data["total_step"] = len(task_list)
        return task_list

    # ---------------- 执行分发 ----------------
    def run_single_step(self, step_info):
        action = Action.from_dict(step_info)
        t0 = time.time()
        try:
            # T1 直连层优先：若步骤本身是 api/cli/sql，或 browser/gui 有 t1_ref
            if action.type in ("api", "cli", "sql"):
                self._run_t1(action)
            elif action.type == "browser":
                # 先试 T1（若有 t1_ref），成功则跳过 T2
                if self.tier_resolver.has_t1(step_info):
                    t1_action = self.tier_resolver.resolve_t1(step_info)
                    if t1_action:
                        try:
                            self._run_t1(t1_action)
                            dur = int((time.time() - t0) * 1000)
                            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur,
                                              extra={"tier": "T1", "t1_func": t1_action.func})
                            return True
                        except Exception as e:
                            logging.info("T1 降级 -> T2: %s" % e)
                            # T1 失败，降级到 T2
                self._run_browser(action)
            elif action.type == "gui":
                # 同上：先试 T1，失败降级到 T3/T4
                if self.tier_resolver.has_t1(step_info):
                    t1_action = self.tier_resolver.resolve_t1(step_info)
                    if t1_action:
                        try:
                            self._run_t1(t1_action)
                            dur = int((time.time() - t0) * 1000)
                            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur,
                                              extra={"tier": "T1", "t1_func": t1_action.func})
                            return True
                        except Exception as e:
                            logging.info("T1 降级 -> T3/T4: %s" % e)
                self._run_gui(action)
            elif action.type == "component":
                self._run_component(action)
            else:
                raise RuntimeError("未知动作类型: %s" % action.type)
            dur = int((time.time() - t0) * 1000)
            tier = self.tier_resolver.describe_tier(step_info)
            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur,
                              extra={"tier": tier})
            return True
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            self.oplog.record(self.break_data.get("current_step", 0), action, "fail", dur,
                              extra={"error": str(e)})
            raise

    def _resolve_sel(self, action, fallback):
        """若动作含 element_ref，到元素库解析最稳定位器；失败回退到内联选择器。"""
        if action.element_ref:
            el = self.repo.get(action.element_ref)
            if el:
                sel = self.browser.resolve_locator(el)
                if sel:
                    self.repo.inc_used(action.element_ref)
                    return sel
                logging.warning("元素库定位失败，回退内联选择器: %s" % action.element_ref)
        return fallback

    @staticmethod
    def _clean_keys(keys):
        """把 Ctrl+字母产生的控制字符（\\x03 等）还原成可读字母，避免日志乱码。"""
        out = []
        for k in keys:
            if isinstance(k, str) and len(k) == 1 and 0 < ord(k) < 0x20:
                out.append(chr(ord(k) + 0x40))
            else:
                out.append(k)
        return out

    def _step_desc(self, step):
        """生成一步的人读描述（解析 element_ref 元素名，避免点击打印空白）。"""
        func = step.get("func", "?")
        args = step.get("args", step.get("params", []))
        ref = step.get("element_ref")
        el_name = ""
        if ref:
            el = self.repo.get(ref)
            if el:
                el_name = el.get("name", "")
        app = step.get("app", "")
        if func == "open_url":
            return "打开网页 %s" % (args[0] if args else "")
        if func in ("click_elem", "hover", "upload_file"):
            return "%s【%s】" % (func, el_name or "元素")
        if func == "input_text":
            txt = args[1] if len(args) > 1 else (args[0] if args else "")
            return "录入「%s」→【%s】" % (txt, el_name or "元素")
        if func in ("click_at", "double_click_at", "right_click_at", "hover_at"):
            return "%s(%s, %s)【%s】" % (func, args[0], args[1], app or "桌面")
        if func == "open_software":
            return "切换/启动 %s" % (os.path.basename(args[0]) if args else "")
        if func in ("press_keys", "key_press"):
            ks = self._clean_keys(args[0]) if args and args[0] else []
            return "按键 %s" % ("+".join(ks) if ks else "")
        if func == "drag_move":
            return "拖拽(%s,%s)→(%s,%s)" % (args[0], args[1], args[2], args[3])
        if func == "drag":
            return "拖拽 %s → %s" % (args[0], args[1])
        # ---- T1 直连层动作 ----
        if func == "call_api":
            return "API 调用 %s" % (args[0] if args else "")
        if func == "run_com":
            return "COM %s.%s" % (args[0] if len(args) > 0 else "", args[1] if len(args) > 1 else "")
        if func == "run_ps":
            return "PowerShell %s" % (str(args[0])[:40] if args else "")
        if func == "run_template":
            return "CLI 模板 %s" % (args[0] if args else "")
        if func in ("query", "execute", "transaction"):
            label = {"query": "SQL 查询", "execute": "SQL 执行",
                     "transaction": "SQL 事务"}[func]
            sql = args[0] if args else ""
            if isinstance(sql, str):
                sql = sql.strip().split("\n")[0][:40]
            return "%s: %s" % (label, sql)
        return func

    def _run_browser(self, action):
        func = action.func
        p = action.params
        if func == "open_url":
            self.browser.open_url(p[0])
        elif func == "input_text":
            sel = self._resolve_sel(action, p[0] if len(p) > 0 else None)
            text = p[1] if len(p) > 1 else ""
            self.browser.input_text(sel, text)
        elif func == "click_elem":
            sel = self._resolve_sel(action, p[0] if len(p) > 0 else None)
            self.browser.click_elem(sel)
        elif func == "hover":
            sel = self._resolve_sel(action, p[0] if len(p) > 0 else None)
            self.browser.hover(sel)
        elif func == "drag":
            self.browser.drag(p[0], p[1])
        elif func == "key_press":
            self.browser.key_press(p[0])
        elif func == "upload_file":
            sel = self._resolve_sel(action, p[0] if len(p) > 0 else None)
            self.browser.upload_file(sel, p[1] if len(p) > 1 else [])
        else:
            getattr(self.browser, func)(*p)

    def _run_gui(self, action):
        getattr(self.gui, action.func)(*action.params)

    def _run_cli(self, action):
        res = cli_executor.execute(action, registry=self.cli_registry)
        if res["rc"] != 0:
            raise RuntimeError("CLI 执行失败(rc=%s): %s" % (res["rc"], res.get("stderr", "")[:200]))
        logging.info("CLI 输出: %s" % res.get("stdout", "")[:200])

    def _run_t1(self, action):
        """T1 直连层统一执行入口（api/cli/sql）。"""
        if action.type == "api":
            self._run_api(action)
        elif action.type == "cli":
            self._run_cli(action)
        elif action.type == "sql":
            self._run_sql(action)
        else:
            raise RuntimeError("T1 不支持的动作类型: %s" % action.type)

    def _run_api(self, action):
        """执行 API 调用（T1 直连层）。"""
        from core.api_client import ApiClient
        client = ApiClient(registry=self.api_registry.templates if self.api_registry else {})
        api_name = action.params[0] if action.params else ""
        overrides = action.params[1] if len(action.params) > 1 else {}
        cred_ref = getattr(action, "credential_ref", None) or action.params[2] if len(action.params) > 2 else None
        res = client.call(api_name, credential_ref=cred_ref, overrides=overrides)
        if res.get("rc") != 0:
            raise RuntimeError("API 调用失败: %s" % res.get("error", "")[:200])
        status = res.get("status", 0)
        if status and not (200 <= status < 300):
            raise RuntimeError("API 返回非 2xx: %s" % status)
        logging.info("API %s -> %s (%dms)" % (api_name, status, res.get("elapsed_ms", 0)))

    def _run_sql(self, action):
        """执行 SQL 操作（T1 直连层）。"""
        from core.db_client import execute as db_execute
        cred_ref = getattr(action, "credential_ref", None)
        res = db_execute(action, registry=self.db_registry, credential_ref=cred_ref)
        if res.get("rc") != 0:
            raise RuntimeError("SQL 执行失败: %s" % res.get("error", "")[:200])
        logging.info("SQL %s -> %s 行 (%dms)" % (action.func, res.get("row_count",
                     res.get("rows_affected", 0)), res.get("elapsed_ms", 0)))

    def _run_component(self, action):
        name = action.params[0]
        kwargs = action.params[1] if len(action.params) > 1 else {}
        comp = comp_mod.get_component(name)
        if not comp:
            raise RuntimeError("组件未找到: %s" % name)
        body = comp_mod.render(comp.get("body", ""), kwargs)
        lang = comp.get("lang", "python")
        if lang == "python":
            res = cli_executor.run_python(body)
            if res["rc"] != 0:
                raise RuntimeError("组件 %s 执行失败: %s" % (name, res["stderr"][:200]))
            logging.info("组件 %s 输出: %s" % (name, res["stdout"][:200]))
        elif lang == "javascript":
            self.browser.send_cmd("Runtime.evaluate",
                                  {"expression": body, "returnByValue": True})
        elif lang == "flow":
            for sub in comp.get("body", []):
                self.run_single_step(sub)
        else:
            raise RuntimeError("组件 %s 不支持的语言: %s" % (name, lang))

    def start_run(self):
        task_flow = self.load_task_flow()
        start_idx = self.break_data["current_step"]
        if start_idx > 0:
            print("⏯ 断点续跑：从步骤 %d/%d 继续" % (start_idx, len(task_flow)))
        else:
            print("▶ 开始回放：共 %d 步" % len(task_flow))

        for idx in range(start_idx, len(task_flow)):
            step = task_flow[idx]
            # 支持单步禁用（RPA 编辑器风格）：enabled 置 false 即跳过
            if step.get("enabled") is False:
                print("  ⏭️ 步骤 %d/%d  已禁用，跳过" % (idx + 1, len(task_flow)))
                self.save_breakpoint(idx + 1, "running")
                continue
            desc = self._step_desc(step)
            tier = self.tier_resolver.describe_tier(step)
            retry = 0
            success = False
            err_info = ""
            while retry < self.max_retry:
                try:
                    self.run_single_step(task_flow[idx])
                    success = True
                    self.save_breakpoint(idx + 1, "running")
                    print("  \u2705 \u6b65\u9aa4 %d/%d  [%s] {%s}" % (idx + 1, len(task_flow), desc, tier))
                    time.sleep(self.cfg["delay_base"])
                    logging.info("步骤%d执行成功" % idx)
                    break
                except Exception as e:
                    retry += 1
                    err_info = "步骤%d异常：%s" % (idx, str(e))
                    logging.error(err_info)
                    print("  \u26a0\ufe0f \u6b65\u9aa4 %d/%d  [%s] {%s} \u91cd\u8bd5 %d/%d\uff1a%s"
                          % (idx + 1, len(task_flow), desc, tier, retry, self.max_retry, str(e)[:60]))
                    time.sleep(2)
            if not success:
                self.save_breakpoint(idx, "error", err_info)
                self.oplog.save()
                print("\n❌ 步骤 %d 多次重试失败，任务暂停。可用 `python main_task.py --reset` 重置后重跑。" % (idx + 1))
                print("   失败详情已写入 run_log.log")
                return
        self.save_breakpoint(0, "finish")
        self.oplog.save()
        print("\n🎉 全部 %d 步执行完成。" % len(task_flow))


if __name__ == "__main__":
    import sys
    # 支持 python main_task.py --reset 清空断点，从头重跑
    if "--reset" in sys.argv:
        bp_path = "breakpoint.json"
        if os.path.exists(bp_path):
            os.remove(bp_path)
            print("已重置断点，将从第 0 步重新回放。")
        else:
            print("无断点文件，直接从头回放。")
    runner = BreakPointTaskRunner()
    runner.start_run()
