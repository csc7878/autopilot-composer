# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 任务编排入口（播放器）【三合一升级版】

支持四类原子动作：
  - browser : 浏览器 CDP 引擎（元素库解析 + 多策略定位回退）
  - gui     : 桌面 GUI 引擎（坐标 / UIA）
  - cli     : 代码执行器（Python / Bash，「编码即动作」）
  - component : 复用组件（参数化 JS/Py/子流程）

播放时：
  1) 加载 elements.json 到 ElementRepository；
  2) 每步若含 element_ref，先到元素库解析出最稳定位器再执行，失败自动回退；
  3) 全程写 operation_log.json（审计 / 流程挖掘）；
  4) 保留断点续跑 + 自动重试。
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

    def load_config(self):
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)

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
            if action.type == "browser":
                self._run_browser(action)
            elif action.type == "gui":
                self._run_gui(action)
            elif action.type == "cli":
                self._run_cli(action)
            elif action.type == "component":
                self._run_component(action)
            else:
                raise RuntimeError("未知动作类型: %s" % action.type)
            dur = int((time.time() - t0) * 1000)
            self.oplog.record(self.break_data.get("current_step", 0), action, "success", dur)
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
        res = cli_executor.execute(action)
        if res["rc"] != 0:
            raise RuntimeError("CLI 执行失败(rc=%s): %s" % (res["rc"], res["stderr"][:200]))
        logging.info("CLI 输出: %s" % res["stdout"][:200])

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
            desc = step.get("func", step.get("type", "?"))
            args_preview = ", ".join(str(a) for a in step.get("args", []))[:40]
            retry = 0
            success = False
            err_info = ""
            while retry < self.max_retry:
                try:
                    self.run_single_step(task_flow[idx])
                    success = True
                    self.save_breakpoint(idx + 1, "running")
                    print("  ✅ 步骤 %d/%d  [%s %s]" % (idx + 1, len(task_flow), desc, args_preview))
                    time.sleep(self.cfg["delay_base"])
                    logging.info("步骤%d执行成功" % idx)
                    break
                except Exception as e:
                    retry += 1
                    err_info = "步骤%d异常：%s" % (idx, str(e))
                    logging.error(err_info)
                    print("  ⚠️ 步骤 %d/%d  [%s] 重试 %d/%d：%s"
                          % (idx + 1, len(task_flow), desc, retry, self.max_retry, str(e)[:60]))
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
