# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 对话驱动入口（chat mode）

把技能从「命令行脚本」升级为「对话即可用」：

  用户说「打开 https://www.baidu.com」  → 拉起自带浏览器并导航
  用户说「开始录制」                  → 开启带 GUI 弹窗确认的交互录制
  用户说「停止录制」                  → 结束录制并落盘
  用户说「回放」 / 「播放」           → 用 task_flow.json 回放

意图解析是轻量的正则/关键词匹配（离线可用，不依赖大模型），便于嵌入专家/对话外壳。

用法：
  python chat_mode.py "打开 https://www.baidu.com"
  python chat_mode.py "开始录制"
  python chat_mode.py "停止"
  python chat_mode.py "回放"
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_launcher import launch
import recorder as rec_mod


# ---------------------------------------------------------------------------
# 意图解析
# ---------------------------------------------------------------------------
def parse_intent(text):
    """把一句话解析为 (intent, params)。意图枚举：open / record / stop / replay / unknown。"""
    t = (text or "").strip()

    # 打开网址
    m = re.search(r"(?:打开|访问|open|goto|navigate)\s*(.+)", t, re.IGNORECASE)
    if m:
        url = m.group(1).strip().strip("\"'")
        # 提取 URL（http/https 或 看起来像域名的）
        url_m = re.search(r"https?://\S+|(?:[\w-]+\.)+[\w]{2,}(?:/\S*)?", url)
        if url_m:
            target = url_m.group(0)
            if not target.startswith("http"):
                target = "https://" + target
            return "open", {"url": target}

    if re.search(r"开始录制|录制|record|录制开始", t, re.IGNORECASE):
        return "record", {}
    if re.search(r"停止|stop|结束录制|结束", t, re.IGNORECASE):
        return "stop", {}
    if re.search(r"回放|播放|重放|replay|run|执行", t, re.IGNORECASE):
        return "replay", {}
    return "unknown", {}


# ---------------------------------------------------------------------------
# 对话驱动主流程
# ---------------------------------------------------------------------------
class ChatDriver:
    def __init__(self, port=9222):
        self.port = port
        self.interactive = None   # InteractiveRecorder 实例
        self.launched = None

    def ensure_browser(self, url=None):
        """拉起（或复用）自带浏览器，必要时导航到 url。"""
        self.launched = launch(port=self.port, open_url=url, reuse=True)
        return self.launched

    def open(self, url):
        from cdp_engine import CdpBrowserCtrl
        self.ensure_browser()
        ctrl = CdpBrowserCtrl(self.port)
        ctrl.open_url(url)
        return "已在自带浏览器打开：%s" % url

    def start_record(self, confirm=True):
        if self.interactive and not self.interactive._stop:
            return "已经在录制中。"
        self.interactive = rec_mod.InteractiveRecorder(port=self.port, confirm=confirm)
        # 取当前页面地址作为归档 host（若已打开）
        cur = self._current_url()
        workdir = self.interactive.prepare(first_url=cur)
        t = threading.Thread(target=self.interactive.run_loop, daemon=True)
        t.start()
        return ("✅ 已开始录制，产物目录：%s\n"
                "   在浏览器里操作，每一步会弹窗确认是否记录；"
                "对话里说「停止」即可结束。" % workdir)

    def stop_record(self):
        if not self.interactive:
            return "当前没有正在进行的录制。"
        self.interactive.stop()
        time.sleep(0.5)
        wd = self.interactive.workdir
        self.interactive = None
        return "录制已停止，SOP 与元素库已写入：%s" % wd

    def replay(self, workdir=None):
        """回放最近一次（或指定）录制产物。"""
        if workdir is None:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
            if not os.path.isdir(base):
                return "还没有录制产物。"
            dirs = sorted(os.listdir(base), reverse=True)
            if not dirs:
                return "还没有录制产物。"
            workdir = os.path.join(base, dirs[0])
        tf = os.path.join(workdir, "task_flow.json")
        if not os.path.exists(tf):
            return "未找到 %s" % tf

        # 复用 main_task 的回放逻辑（把 task_flow_path 指向该目录）
        import main_task
        runner = main_task.BreakPointTaskRunner.__new__(main_task.BreakPointTaskRunner)
        runner.cfg = runner.load_config()
        runner.break_data = {"current_step": 0, "total_step": 100, "task_status": "stop"}
        runner.max_retry = runner.cfg.get("max_retry", 3)
        runner.task_flow_path = tf
        from core.element_repo import ElementRepository
        runner.repo = ElementRepository.load(os.path.join(workdir, "elements.json"))
        preset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preset_elements.json")
        if os.path.exists(preset_path):
            runner.repo.merge(ElementRepository.load_preset(preset_path))
        import core.op_log as op_log_mod
        runner.oplog = op_log_mod.OperationLog(os.path.join(workdir, "operation_log.json"))
        try:
            from core import components as comp_mod
            comp_mod.set_component_dir(runner.cfg.get("components_dir",
                                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "components")))
            runner.comp_mod = comp_mod
        except Exception:
            pass
        runner.gui = None

        # 确保浏览器在线
        self.ensure_browser()
        from cdp_engine import CdpBrowserCtrl

        task_flow = runner.load_task_flow()
        # 若 task_flow 首步是 open_url，连到对应页面并导航，确保操作作用在正确的 tab
        first = task_flow[0] if task_flow else {}
        runner.browser = CdpBrowserCtrl(self.port)
        if first.get("func") == "open_url" and first.get("args"):
            try:
                runner.browser.connect(url_filter=first["args"][0])
                runner.browser.open_url(first["args"][0])
            except Exception:
                pass
                pass
        ok = 0
        for idx in range(len(task_flow)):
            try:
                runner.run_single_step(task_flow[idx])
                ok += 1
            except Exception as e:
                return "回放在第 %d 步失败：%s" % (idx, e)
        runner.oplog.save()
        return "✅ 回放完成：%d/%d 步成功（%s）" % (ok, len(task_flow), workdir)

    def _current_url(self):
        """从 CDP 取当前激活页面 URL。"""
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen("http://127.0.0.1:%d/json" % self.port, timeout=3) as r:
                tabs = _json.load(r)
            for t in tabs:
                if t.get("type") == "page":
                    return t.get("url")
        except Exception:
            pass
        return None

    def dispatch(self, text):
        intent, params = parse_intent(text)
        if intent == "open":
            return self.open(params["url"])
        if intent == "record":
            return self.start_record()
        if intent == "stop":
            return self.stop_record()
        if intent == "replay":
            return self.replay()
        return ("🤖 我能做的：\n"
                "  • 「打开 <网址>」 —— 拉起自带浏览器并导航\n"
                "  • 「开始录制」 —— 开启带确认弹窗的录制\n"
                "  • 「停止」 —— 结束录制并落盘\n"
                "  • 「回放」 —— 回放最近一次录制")


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    if not text:
        print(__doc__)
        return
    driver = ChatDriver()
    print(driver.dispatch(text))


if __name__ == "__main__":
    main()
