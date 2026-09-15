# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 任务编排入口（播放器）【四层自动化 + 结果校验 v3.4.1】

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

v3.4.1 新增两项可靠性机制：
  1) 结果校验（Verify）：步骤可写 verify 规格，执行后校验「是否真的生效」，
     未通过则按失败处理 → 重试 / T1 降级 / 断点暂停（见 core/verify.py）。
  2) 元素库健康度 + 自动遗忘：定位器命中/未命中计数，连续失败的策略自动
     stale（跳过），元素连续 15 次未命中标记待复核，任一命中即复活
     （见 core/element_repo.py，用 `python main_task.py --health` 查看）。

v3.5.0 新增「置信度驱动的自适应执行」——借鉴 OS-Kairos（ACL 2025 Findings,
arXiv:2503.16465）的核心洞察：GUI 智能体最大的可靠性问题是 **过度执行**，
即缺乏把握时仍全自动盲目执行；论文实证「何时介入」比「要不要介入」更关键
（自适应介入 88.20% vs 每步都问人 62% vs 全自动 14.29%）。本版把该机制移植
到确定性 RPA，**不依赖任何模型**：
  1) 确定性置信度（core/confidence.py）：用 Tier 层级、定位器唯一性/策略、
     元素库健康度、步骤历史成功率、是否配校验、环境状态合成 0~5 分，
     三项都能追溯到具体信号 —— 可解释、可复现、零算力。
  2) 环境守卫（core/env_guard.py）：识别「被踢回登录页 / 跳到错误页 /
     意外弹窗 / 调试通道断开」这类环境劫持（最隐蔽的故障：后续步骤会在
     错误页面上假装成功），并尝试轻量自愈。
  3) 策略轮换重试（本文件 _switch_strategy）：重试不再原样重跑同一个动作，
     而是逐轮换做法——换定位器策略 → 环境修复后重新定位，直击「盲目执行」。
  4) 检查点回滚（core/checkpoint.py）：记录每步成功后的页面状态，失败诊断
     时回退到上一个已知良好状态，避免在错误路径上继续叠加错误。
  5) 步骤战绩（core/step_stats.py）：按步骤指纹累计成功/失败次数，给置信度
     提供「同一份流程反复回放的真实战绩」这一模型没有的先验。
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
from core.verify import Verifier
from core.confidence import ConfidenceScorer, parse_tier
from core.env_guard import EnvironmentGuard
from core.step_stats import StepStats
from core.checkpoint import CheckpointStore
from confirm_box import ask_choice


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
        self.elements_path = elements_path
        # 结果校验器（v3.4.1）：每步执行后校验是否真的生效
        self.verifier = Verifier(browser=self.browser,
                                 timeout=self.cfg.get("verify_timeout", 5.0),
                                 interval=self.cfg.get("verify_interval", 0.3))
        self._verify_count = 0        # 本次运行通过的校验项数
        self._verify_skipped = 0
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

        # ---- v3.5.0 可靠性四件套 ----
        # 步骤战绩（置信度的历史先验：同一份流程反复回放的真实成功/失败次数）
        self.stats = StepStats.load(
            self.cfg.get("step_stats_path", "./step_stats.json"),
            min_samples=self.cfg.get("stats_min_samples", 2))
        # 环境守卫（登录页过期 / 错误页 / 意外弹窗 / 调试通道断开）
        self.env_guard = EnvironmentGuard(self.cfg.get("env_guard") or {})
        # 置信度评分器（确定性版的自适应人机协作，零模型）
        self.scorer = ConfidenceScorer(self.cfg.get("confidence") or {},
                                       repo=self.repo, stats=self.stats,
                                       env_guard=self.env_guard)
        # 检查点（用于「陷入错误路径」时的回滚）
        self.ck_cfg = self.cfg.get("checkpoint") or {}
        self.checkpoints = CheckpointStore(
            self.cfg.get("checkpoint_path", "./checkpoints.json"),
            keep=self.ck_cfg.get("keep", 20))

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

    # ---------------- v3.5.0：置信度信号预探测 ----------------
    def _probe_signals(self, step, action):
        """执行前收集置信度评分所需的确定性信号。

        顺带把「将要使用的定位器」缓存到 action._pre_sel，供执行阶段复用 ——
        否则同一步会为评分多做一轮 CDP 定位，白白变慢。
        """
        sig = {
            "tier": parse_tier(self.tier_resolver.describe_tier(step)),
            "has_verify": bool(step.get("verify")),
            "history_rate": self.stats.success_rate(step),
            "probe": {},
            "element": None,
            "env": {"kind": "ok", "skipped": True},
        }
        # 环境浅检只对浏览器步骤做（桌面步骤可能根本没有浏览器在跑）
        if action.type == "browser":
            try:
                sig["env"] = self.env_guard.check(self.browser, deep=False)
            except Exception:
                sig["env"] = {"kind": "ok", "skipped": True}

        if action.type != "browser":
            return sig

        el = self.repo.get(action.element_ref) if action.element_ref else None
        if el:
            sig["element"] = el
            try:
                sel = self.browser.resolve_locator(el)
            except Exception:
                sel = None
            info = getattr(self.browser, "last_resolve", None) or {}
            if sel:
                action._pre_sel = sel
                strat = info.get("strategy") or self._strategy_of(el, sel)
                # 预先把「将要使用的策略」记下来，供策略轮换与校验失败归因复用
                action._resolved_strategy = strat
                sig["probe"] = {
                    "found": True,
                    "count": info.get("count") or 1,
                    "strategy": strat,
                    "source": "repo",
                    "used_stale": bool(info.get("used_stale")),
                }
            else:
                sig["probe"] = {"found": False, "source": "repo"}
        elif action.params and action.func in ("click_elem", "input_text",
                                               "hover", "upload_file"):
            # 没有 element_ref：探测内联选择器的匹配数量（衡量回退路径的质量）
            sel0 = action.params[0]
            if isinstance(sel0, str) and sel0:
                try:
                    p = self.browser._probe(sel0)
                    sig["probe"] = {"found": bool(p.get("found")),
                                    "count": int(p.get("count") or 0),
                                    "source": "inline"}
                except Exception:
                    sig["probe"] = {"source": "inline"}
        return sig

    # ---------------- 执行分发 ----------------
    def _verify(self, action, step_info):
        """执行步骤级结果校验（v3.4.1）。

        校验未通过 → 抛 RuntimeError，由外层重试/降级逻辑接管：
          - browser/gui 步骤：T1 路径的校验失败会触发降级到 T2/T3/T4
          - 全部路径都失败 → 该步按失败处理，写入断点并暂停
        校验失败同时视为「定位器选错了」的强信号，计入元素库 miss。
        """
        spec = getattr(action, "verify", None) or (step_info or {}).get("verify")
        if not spec:
            return None
        res = self.verifier.check(spec, browser=self.browser)
        if res.get("skipped"):
            self._verify_skipped += 1
            logging.info("步骤结果校验跳过：%s" % res.get("detail", ""))
            return res
        if not res.get("ok"):
            # 校验不过 = 动作没生效：若走的是元素库定位，记一次 miss
            if getattr(action, "element_ref", None):
                self.repo.record_miss(action.element_ref,
                                      getattr(action, "_resolved_strategy", None))
            raise RuntimeError("结果校验未通过：%s" % res.get("detail", ""))
        self._verify_count += len([r for r in res.get("results", []) if not r.get("skipped")])
        logging.info("步骤结果校验通过：%s" % res.get("detail", ""))
        return res

    def run_single_step(self, step_info, action=None):
        # v3.5.0：允许调用方传入既有 action 对象，让置信度预探测缓存的定位器
        # （action._pre_sel）能在执行阶段复用，避免重复定位。
        if action is None:
            action = Action.from_dict(step_info)
        t0 = time.time()
        try:
            # T1 直连层优先：若步骤本身是 api/cli/sql，或 browser/gui 有 t1_ref
            if action.type in ("api", "cli", "sql"):
                self._run_t1(action)
                self._verify(action, step_info)
            elif action.type == "browser":
                # 先试 T1（若有 t1_ref），成功且校验通过则跳过 T2
                if self.tier_resolver.has_t1(step_info):
                    t1_action = self.tier_resolver.resolve_t1(step_info)
                    if t1_action:
                        try:
                            self._run_t1(t1_action)
                            self._verify(action, step_info)
                            dur = int((time.time() - t0) * 1000)
                            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur,
                                              extra={"tier": "T1", "t1_func": t1_action.func})
                            return True
                        except Exception as e:
                            logging.info("T1 降级 -> T2: %s" % e)
                            # T1 失败或校验不过，降级到 T2
                self._run_browser(action)
                self._verify(action, step_info)
            elif action.type == "gui":
                # 同上：先试 T1，失败降级到 T3/T4
                if self.tier_resolver.has_t1(step_info):
                    t1_action = self.tier_resolver.resolve_t1(step_info)
                    if t1_action:
                        try:
                            self._run_t1(t1_action)
                            self._verify(action, step_info)
                            dur = int((time.time() - t0) * 1000)
                            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur,
                                              extra={"tier": "T1", "t1_func": t1_action.func})
                            return True
                        except Exception as e:
                            logging.info("T1 降级 -> T3/T4: %s" % e)
                self._run_gui(action)
                self._verify(action, step_info)
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
        """若动作含 element_ref，到元素库解析最稳定位器；失败回退到内联选择器。

        v3.4.1：解析结果计入元素库健康度——
          - 命中 → record_hit（miss_streak 归零，复活已遗忘元素）
          - 未命中 → record_miss（连续 miss 达阈值即标记 stale，回放时提示复核）
        """
        # v3.5.0：复用执行前预探测的结果（置信度评分已经定位过一次），
        # 避免同一步为评分而多做一轮 CDP 定位
        pre = getattr(action, "_pre_sel", None)
        if pre:
            action._pre_sel = None
            return pre
        if action.element_ref:
            el = self.repo.get(action.element_ref)
            if el:
                if el.get("stale"):
                    msg = "元素已标记待复核（连续未命中）：%s" % el.get("name", action.element_ref)
                    logging.warning(msg)
                    print("     \u26a0\ufe0f %s" % msg)
                sel = self.browser.resolve_locator(el)
                if sel:
                    self.repo.inc_used(action.element_ref)
                    if self.repo.record_hit(action.element_ref):
                        logging.info("元素 %s 已复活（重新命中）" % el.get("name", ""))
                    # 记录本次实际选中的策略，供校验失败时归因
                    action._resolved_strategy = self._strategy_of(el, sel)
                    return sel
                self.repo.record_miss(action.element_ref)
                logging.warning("元素库定位失败，回退内联选择器: %s" % action.element_ref)
        return fallback

    @staticmethod
    def _strategy_of(element, query):
        """反查某 query 对应的定位器策略名（用于健康度归因）。"""
        for l in element.get("locators", []) or []:
            if l.get("query") == query:
                return l.get("strategy")
        return None

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

    def _save_repo(self):
        """把元素库健康度落盘（不写入预置库条目）。"""
        try:
            self.repo.save(skip_presets=True)
        except Exception as e:
            logging.warning("元素库落盘失败: %s" % e)

    # ---------------- v3.5.0：置信度驱动的自适应执行 ----------------
    def _announce_modes(self):
        """启动时说明当前处于哪种可靠性模式（不让用户猜）。"""
        m = self.scorer.mode
        if not self.scorer.enabled or m == "off":
            print("   置信度评估：关闭")
        elif m == "shadow":
            print("   置信度评估：观察模式 —— 只评分与告警，绝不打断（想让它在低置信时"
                  "弹窗问你，把 config.json 的 confidence.mode 改成 adaptive）")
        else:
            print("   置信度评估：自适应介入 —— 低置信会弹窗确认，极低置信直接暂停")
        if self.ck_cfg.get("enabled", True):
            print("   环境守卫 + 检查点回滚：已启用")

    def _maybe_intervene(self, conf, desc, idx, total):
        """置信度驱动的自适应介入。返回 continue / skip / pause。

        这就是 OS-Kairos 的 confidence-driven interaction：把「要不要继续」
        的决定权，在**该犹豫的那几步**交还给人类 —— 不是每步都问，也不是
        从不问（论文实测：每步都问 62% vs 自适应 88.20% vs 全自动 14.29%）。
        """
        if not conf:
            return "continue"
        act = conf["action"]
        low = conf["level"] in ("low", "critical")
        streak_hit = self.scorer.note_low(low)
        if act == "auto":
            if streak_hit:
                print("     ⚠️ 已连续 %d 步置信度偏低，建议停下来看看"
                      "（`python main_task.py --health` 可体检元素库）。"
                      % self.scorer.low_streak)
            return "continue"

        headline = "置信 %.2f/5 %s —— 这一步可能不可靠" % (conf["score"], conf["level_label"])
        lines = list(conf.get("explain") or [])
        if act == "abort":
            # 极低置信：连「继续执行」都不提供，只允许跳过或暂停
            options = [{"label": "跳过这步", "value": "skip"},
                       {"label": "暂停任务", "value": "pause"}]
            default_index = 1
            message = ("步骤 %d/%d：%s\n\n置信度过低（存在不可逆风险），默认不执行。"
                       % (idx + 1, total, desc))
        else:
            options = [{"label": "继续执行", "value": "continue"},
                       {"label": "跳过这步", "value": "skip"},
                       {"label": "暂停任务", "value": "pause"}]
            default_index = 0
            message = "步骤 %d/%d：%s" % (idx + 1, total, desc)
        self.scorer.interventions += 1
        choice = ask_choice(message, title="AutoPilot 回放确认", options=options,
                            timeout=self.scorer.interactive_timeout,
                            detail_lines=lines, headline=headline,
                            default_index=default_index)
        logging.info("人工介入决策：步骤%d → %s（置信 %.2f / %s）"
                     % (idx + 1, choice, conf["score"], conf["level"]))
        return choice if choice in ("skip", "pause") else "continue"

    def _after_step_ok(self, idx, action, desc):
        """步骤成功后的收尾：断点 + 检查点。"""
        self.save_breakpoint(idx + 1, "running")
        if self.ck_cfg.get("enabled", True):
            url = ""
            if action.type == "browser":
                try:
                    url = self.browser.current_url()
                except Exception:
                    url = ""
            self.checkpoints.record_ok(idx + 1, url=url, desc=desc)

    def _switch_strategy(self, action, attempt, idx, total, env_kind="ok"):
        """策略轮换：把该步换成**另一种做法**。返回 True 表示确实换了。

        直击 OS-Kairos 批评的 over-execution ——「在不确定时反复执行同一个
        动作」既浪费时间又可能造成不可逆后果。

        轮换顺序是**自适应**的：环境明显异常时先修环境（在登录页/错误页上
        换定位器毫无意义），环境正常时才先换定位器（那多半是元素本身变了）。
        两类候选：
          repair   —— 环境修复（关闭弹窗 / 回滚页面 / 重载）
          relocate —— 换一个定位器策略（避开刚失败的那个）
        """
        if action.type != "browser" or not action.element_ref:
            return False
        el = self.repo.get(action.element_ref)
        if not el:
            return False

        order = (["repair", "relocate"] if env_kind not in (None, "ok")
                 else ["relocate", "repair"])
        pick = order[attempt - 1] if attempt - 1 < len(order) else order[-1]

        if pick == "repair":
            if self._repair_environment(idx, total):
                action._pre_sel = None
                action._resolved_strategy = None
                return True
            pick = "relocate"   # 环境修不了，退而换定位器

        info = getattr(self.browser, "last_resolve", None) or {}
        used = getattr(action, "_resolved_strategy", None) or info.get("strategy")
        avoid = {used} if used else set()
        try:
            sel = self.browser.resolve_locator(el, avoid=avoid)
        except Exception:
            sel = None
        if not sel:
            return False
        new_strat = (getattr(self.browser, "last_resolve", None) or {}).get("strategy")
        action._pre_sel = sel
        action._resolved_strategy = None
        print("     ↻ 换一种定位方式重试：「%s」→「%s」"
              % (used or "?", new_strat or "?"))
        return True

    def _repair_environment(self, idx, total):
        """尝试把环境恢复到可用状态。返回是否真的做了动作。"""
        kind = "ok"
        try:
            kind = (self.env_guard.check(self.browser, deep=True) or {}).get("kind") or "ok"
        except Exception:
            kind = "ok"
        if kind != "ok":
            ck = self.checkpoints.last_good(need_url=True)
            r = self.env_guard.recover(self.browser, kind, checkpoint=ck)
            if r.get("ok"):
                print("     ↻ 环境已自愈（%s：%s）后重试" % (r.get("action"), r.get("detail")))
            else:
                print("     ↻ 环境异常未能自动修复（%s），仍尝试重试" % r.get("detail"))
            return True
        # 环境正常但仍失败：动态渲染未就绪很常见，重载一次往往就好
        try:
            if self.browser.current_url():
                self.browser.send_cmd("Page.reload", {"ignoreCache": False})
                time.sleep(1.5)
                print("     ↻ 已重载页面并重新定位后重试")
                return True
        except Exception:
            pass
        return False

    def _diagnose_failure(self, action, idx, err_info, tried_rotation=False):
        """重试全部失败后的诊断：说清「为什么」和「现在该做什么」。"""
        print("\n❌ 步骤 %d 已重试 %d 次仍失败，任务暂停。" % (idx + 1, self.max_retry))
        if err_info:
            print("   最后错误：%s" % str(err_info)[:160])
        kind = "ok"
        if action is not None and action.type == "browser":
            try:
                st = self.env_guard.check(self.browser, deep=True)
                kind = st.get("kind") or "ok"
                if kind != "ok":
                    print("   🔎 环境诊断：%s" % (st.get("detail") or kind))
                    r = self.env_guard.recover(self.browser, kind,
                                               checkpoint=self.checkpoints.last_good(need_url=True))
                    print("   %s 自动恢复：%s —— %s"
                          % ("✅" if r.get("ok") else "⚠️", r.get("action"), r.get("detail")))
                    if r.get("ok"):
                        print("   👉 环境已恢复，直接 `python main_task.py` 就能从该步续跑。")
            except Exception as e:
                print("   🔎 环境诊断失败：%s" % str(e)[:120])
        if kind == "ok":
            print("   🔎 环境诊断：页面状态正常 → 问题更可能出在这一步的元素定位或业务前置条件。")
            if tried_rotation:
                print("   （已尝试过更换定位策略、修复页面环境等替代做法，说明不是偶发抖动）")
            print("   👉 建议：`python main_task.py --health` 体检元素库；确认前置条件后"
                  "`python main_task.py` 从该步续跑。")
        print("   失败详情见 run_log.log。")

    def _persist(self):
        """统一落盘（操作日志 / 元素库 / 步骤战绩 / 检查点）。"""
        for fn in (self.oplog.save, self._save_repo, self.stats.save,
                   self.checkpoints.save):
            try:
                fn()
            except Exception:
                pass

    def _run_step_with_rotation(self, step, action, idx, total, desc, tier, conf):
        """执行一步；失败时**每次换一种做法**重试。返回 (success, err_info)。"""
        attempts = max(1, int(self.max_retry))
        last_err = ""
        for attempt in range(1, attempts + 1):
            try:
                self.run_single_step(step, action)
            except Exception as e:
                last_err = "步骤%d异常：%s" % (idx, str(e))
                logging.error(last_err)
                # 失败也要记战绩：置信度的历史先验就来自这里
                self.stats.record(step, False, desc, err=str(e))
                if attempt >= attempts:
                    break
                env_kind = "ok"
                if conf and conf.get("signals"):
                    env_kind = conf["signals"].get("env") or "ok"
                switched = self._switch_strategy(action, attempt, idx, total,
                                                 env_kind=env_kind)
                if not switched:
                    print("  \u26a0\ufe0f \u6b65\u9aa4 %d/%d  [%s] {%s} \u91cd\u8bd5 %d/%d\uff1a%s"
                          % (idx + 1, total, desc, tier, attempt, attempts, str(e)[:80]))
                time.sleep(1.5)
                continue

            # ---- 成功 ----
            self._after_step_ok(idx, action, desc)
            self.stats.record(step, True, desc)
            tail = ("  " + self.scorer.short(conf)) if conf else ""
            print("  \u2705 \u6b65\u9aa4 %d/%d  [%s] {%s}%s"
                  % (idx + 1, total, desc, tier, tail))
            if step.get("verify"):
                print("     \u2714 \u7ed3\u679c\u6821\u9a8c\u901a\u8fc7")
            time.sleep(self.cfg["delay_base"])
            logging.info("步骤%d执行成功" % idx)
            return True, ""
        return False, last_err

    def start_run(self):
        task_flow = self.load_task_flow()
        start_idx = self.break_data["current_step"]
        if start_idx > 0:
            print("⏯ 断点续跑：从步骤 %d/%d 继续" % (start_idx, len(task_flow)))
        else:
            print("▶ 开始回放：共 %d 步" % len(task_flow))
        self._announce_modes()

        for idx in range(start_idx, len(task_flow)):
            step = task_flow[idx]
            # 支持单步禁用（RPA 编辑器风格）：enabled 置 false 即跳过
            if step.get("enabled") is False:
                print("  ⏭️ 步骤 %d/%d  已禁用，跳过" % (idx + 1, len(task_flow)))
                self.save_breakpoint(idx + 1, "running")
                continue
            desc = self._step_desc(step)
            tier = self.tier_resolver.describe_tier(step)
            action = Action.from_dict(step)

            # ---- 1) 执行前：置信度评分（确定性信号，零模型） ----
            conf = None
            if self.scorer.enabled and self.scorer.mode != "off":
                try:
                    conf = self.scorer.score(step, self._probe_signals(step, action))
                except Exception as e:
                    logging.warning("置信度评分失败（不影响执行）：%s" % e)
            if conf and conf["level"] in ("low", "critical"):
                print("  %s 步骤 %d/%d  [%s] {%s}  %s"
                      % ("\u26d4" if conf["level"] == "critical" else "\u26a0\ufe0f",
                         idx + 1, len(task_flow), desc, tier, self.scorer.short(conf)))
                for ln in self.scorer.detail_lines(conf):
                    print(ln)

            # ---- 2) 自适应介入：该犹豫时把决定权交还给人 ----
            decision = self._maybe_intervene(conf, desc, idx, len(task_flow))
            if decision == "skip":
                print("     ⏭️ 已按你的选择跳过该步")
                self.save_breakpoint(idx + 1, "running")
                continue
            if decision == "pause":
                self.save_breakpoint(idx, "paused",
                                     "置信度不足，用户选择暂停（步骤 %d）" % (idx + 1))
                self._persist()
                print("\n⏸ 已暂停。处理好后执行 `python main_task.py` 从该步续跑。")
                return

            # ---- 3) 执行 + 策略轮换重试 ----
            success, err_info = self._run_step_with_rotation(
                step, action, idx, len(task_flow), desc, tier, conf)

            # 每 20 步落盘一次，避免中途崩溃丢失统计
            if (idx + 1) % 20 == 0:
                self._persist()
            if not success:
                self.save_breakpoint(idx, "error", err_info)
                self._persist()
                self._diagnose_failure(action, idx, err_info, tried_rotation=True)
                return

        self.save_breakpoint(0, "finish")
        self._persist()
        print("\n🎉 全部 %d 步执行完成。" % len(task_flow))
        if self._verify_count or self._verify_skipped:
            print("   结果校验：通过 %d 项，跳过 %d 项"
                  % (self._verify_count, self._verify_skipped))
        # 置信度汇报：把「哪几步心里没底」摊开给用户
        try:
            s = self.scorer.summary()
            if s:
                b = s["buckets"]
                print("   置信度：平均 %.2f/5（高 %d / 中 %d / 偏低 %d / 极低 %d），人工介入 %d 次"
                      % (s["avg"], b["high"], b["medium"], b["low"],
                         b["critical"], s["interventions"]))
                if s["low_count"]:
                    print("   ⚠️ 有 %d 步置信度偏低，建议 `python main_task.py --health`"
                          " 体检元素库" % s["low_count"])
        except Exception:
            pass
        # 元素库健康度提醒（自动遗忘的条目需人工复核）
        try:
            hr = self.repo.health_report()
            if hr["stale"]:
                print("   ⚠️ 元素库有 %d 个元素被自动遗忘（连续未命中），可执行 "
                      "`python main_task.py --health` 查看详情、`--health-reset` 复核后恢复。"
                      % hr["stale"])
        except Exception:
            pass


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

    # --health：打印元素库健康度报告后退出（不跑流程）
    # --health-reset：把被自动遗忘的元素恢复为可用（人工复核后使用）
    if "--health" in sys.argv or "--health-reset" in sys.argv:
        cfg = {}
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        elements_path = cfg.get("elements_path", "./elements.json")
        repo = ElementRepository.load(elements_path)
        if "--health-reset" in sys.argv:
            n = repo.reset_health(only_stale=True)
            repo.save(skip_presets=True)
            print("已恢复 %d 个被自动遗忘的元素（miss_streak 归零）。" % n)
        hr = repo.health_report()
        print("\n元素库健康度：共 %d 个元素，%d 个正常，%d 个待复核"
              % (hr["total"], hr["healthy"], hr["stale"]))
        if hr.get("never_used"):
            print("（其中 %d 个尚未回放过，不计入问题元素）" % hr["never_used"])
        if hr.get("dead_locator_elements"):
            print("定位器自动遗忘：%d 个元素有策略被跳过" % hr["dead_locator_elements"])
        if hr["weakest"]:
            print("\n最需要关注的元素（按连续未命中排序）：")
            for w in hr["weakest"]:
                nm = w["name"][:28] + ("…" if len(w["name"]) > 28 else "")
                dead = ("，已跳过策略：" + ",".join(w["stale_locators"])) if w["stale_locators"] else ""
                print("  - %s（%s）命中 %d 次、连续未命中 %d 次，定位器 %d 个%s"
                      % (nm, w["domain"] or "-", w["hit_count"],
                         w["miss_streak"], w["locators"], dead))
        else:
            print("暂无失败记录（回放过至少一次后才会产生健康数据）。")
        if hr["stale"]:
            print("\n提示：被自动遗忘的元素在回放时会跳过对应定位器，"
                  "确认界面已恢复后执行 `python main_task.py --health-reset` 复活。")

        # 步骤战绩（v3.5.0）：哪几步历史上最容易失败
        try:
            ss = StepStats.load(cfg.get("step_stats_path", "./step_stats.json")).summary()
            if ss["steps"]:
                print("\n步骤战绩：%d 个步骤有记录，累计成功 %d 次 / 失败 %d 次"
                      % (ss["steps"], ss["runs_ok"], ss["runs_fail"]))
                if ss["worst"]:
                    print("最容易失败的步骤：")
                    for w in ss["worst"][:5]:
                        nm = w["desc"] or w["fp"]
                        nm = nm[:30] + ("…" if len(nm) > 30 else "")
                        print("  - %s  成功 %d / 失败 %d（成功率 %.0f%%）"
                              % (nm, w["ok"], w["fail"], w["rate"] * 100))
        except Exception:
            pass
        sys.exit(0)

    runner = BreakPointTaskRunner()
    runner.start_run()
