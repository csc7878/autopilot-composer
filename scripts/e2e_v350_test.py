# -*- coding: utf-8 -*-
"""AutoPilot Composer v3.5.0 离线验证套件 —— 置信度驱动的自适应执行。

全部离线：不连浏览器、不开 GUI、不写用户文件（副作用路径被替换为 no-op）。
运行： cd scripts && ./.venv/Scripts/python.exe e2e_v350_test.py

覆盖：
  A 确定性置信度评分（Tier / 定位器 / 元素健康度 / 历史战绩 / 环境封顶 / 分级 / 模式）
  B 环境守卫（登录页 / 错误页 / 弹窗 / 通道断开 / 不可自动恢复的诚实返回）
  C 步骤战绩（指纹稳定性 / 样本保护 / 持久化）
  D 检查点（最近良好状态 / 环形上限 / 同步骤覆盖）
  E 主循环集成（预探测复用 / 策略轮换 / 自适应介入三选项 / shadow 不打断）
"""
import io
import os
import sys
import json
import contextlib
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.confidence import (ConfidenceScorer, parse_tier,
                             LEVEL_HIGH, LEVEL_LOW, LEVEL_CRITICAL,
                             ACTION_AUTO, ACTION_CONFIRM, ACTION_ABORT)
from core.env_guard import EnvironmentGuard
from core.step_stats import StepStats, step_fingerprint
from core.checkpoint import CheckpointStore

PASS = [0]
FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1
        print("  OK   %s%s" % (name, ("   [%s]" % extra) if extra else ""))
    else:
        FAIL[0] += 1
        print("  FAIL %s%s" % (name, ("   [%s]" % extra) if extra else ""))


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


# ======================================================================
# A. 确定性置信度评分
# ======================================================================
section("A. 置信度评分器 core/confidence.py")

sc = ConfidenceScorer({"enabled": True, "mode": "adaptive"})

r_t1 = sc.score({}, {"tier": "T1", "probe": {"found": True, "count": 1, "strategy": "id"}})
r_t4 = sc.score({}, {"tier": "T4"})
check("A1 T1 直调置信度显著高于 T4 坐标", r_t1["score"] > r_t4["score"] + 2,
      "T1=%.2f T4=%.2f" % (r_t1["score"], r_t4["score"]))
check("A2 Tier 可从描述串解析", parse_tier("T2(cdp)") == "T2" and parse_tier("T1(call_api)") == "T1")

uniq = sc.score({}, {"tier": "T2", "probe": {"found": True, "count": 1, "strategy": "id"}})
multi = sc.score({}, {"tier": "T2", "probe": {"found": True, "count": 3, "strategy": "css_chain"}})
miss = sc.score({}, {"tier": "T2", "probe": {"found": False, "source": "repo"}})
check("A3 唯一命中 > 多匹配 > 未命中", uniq["score"] > multi["score"] > miss["score"],
      "%.2f / %.2f / %.2f" % (uniq["score"], multi["score"], miss["score"]))
check("A4 多匹配给出「可能点错」的可读归因",
      any("点错" in e for e in multi["explain"]))
check("A5 未命中归因明确", any("没有任何定位器" in e for e in miss["explain"]))
check("A6 多匹配被判为需要留意（低于 auto 线或明显掉分）",
      multi["score"] < uniq["score"] - 1.0, "%.2f vs %.2f" % (multi["score"], uniq["score"]))

healthy = sc.score({}, {"tier": "T2", "probe": {"found": True, "count": 1, "strategy": "id"},
                        "element": {"hit_count": 9, "miss_streak": 0, "stale": False}})
sick = sc.score({}, {"tier": "T2", "probe": {"found": True, "count": 1, "strategy": "id"},
                     "element": {"hit_count": 0, "miss_streak": 20, "stale": True}})
check("A7 健康元素 > 已遗忘元素", healthy["score"] > sick["score"],
      "%.2f vs %.2f" % (healthy["score"], sick["score"]))
check("A8 已遗忘元素归因点出连续未命中", any("自动遗忘" in e for e in sick["explain"]))

h0 = sc.score({}, {"tier": "T2", "history_rate": 0.0})
h1 = sc.score({}, {"tier": "T2", "history_rate": 1.0})
check("A9 历史成功率 100% > 0%", h1["score"] > h0["score"], "%.2f vs %.2f" % (h1["score"], h0["score"]))
v_yes = sc.score({}, {"tier": "T2", "has_verify": True})
v_no = sc.score({}, {"tier": "T2"})
check("A10 配了结果校验的更可信", v_yes["score"] > v_no["score"])

env = sc.score({}, {"tier": "T1", "probe": {"found": True, "count": 1, "strategy": "id"},
                    "env": {"kind": "login", "detail": "URL 命中登录特征"}})
check("A11 环境异常可把 T1 也压到封顶值", env["score"] <= 1.21 and env["capped_by"] is not None,
      "score=%.2f cap=%s" % (env["score"], env["capped_by"]))
check("A12 封顶归因写明原因", any("封顶" in e for e in env["explain"]))
cdp_down = sc.score({}, {"tier": "T2", "env": {"kind": "cdp_down", "detail": "通道断了"}})
check("A13 通道断开压到最低档", cdp_down["score"] <= 0.5 and cdp_down["level"] == LEVEL_CRITICAL)

# 分级与动作（adaptive 模式）
best = sc.score({}, {"tier": "T1", "probe": {"found": True, "count": 1, "strategy": "id"},
                     "element": {"hit_count": 5, "miss_streak": 0, "stale": False},
                     "history_rate": 1.0, "has_verify": True})
check("A14 理想步骤 → 高置信 / 自动执行",
      best["level"] == LEVEL_HIGH and best["action"] == ACTION_AUTO,
      "%.2f %s" % (best["score"], best["level"]))
mid = sc.score({}, {"tier": "T3", "probe": {"found": True, "count": 1, "strategy": "css"}})
check("A15 中等置信 → 仍自动（不打扰）", mid["action"] == ACTION_AUTO, "%.2f" % mid["score"])
low = sc.score({}, {"tier": "T2", "probe": {"found": False, "source": "repo"}})
check("A16 偏低置信 → 请求确认（adaptive）",
      low["level"] == LEVEL_LOW and low["action"] == ACTION_CONFIRM, "%.2f" % low["score"])
crit = sc.score({}, {"tier": "T4", "probe": {"found": False},
                     "env": {"kind": "cdp_down", "detail": "断"}})
check("A17 极低置信 → 中止（不执行）", crit["action"] == ACTION_ABORT, "%.2f" % crit["score"])

sc_shadow = ConfidenceScorer({"enabled": True, "mode": "shadow"})
sh = sc_shadow.score({}, {"tier": "T4", "probe": {"found": False},
                          "env": {"kind": "login", "detail": "登录页"}})
check("A18 shadow 模式：极低置信也只告警、绝不打断",
      sh["level"] == LEVEL_CRITICAL and sh["action"] == ACTION_AUTO)
sc_off = ConfidenceScorer({"enabled": False, "mode": "adaptive"})
off = sc_off.score({}, {"tier": "T4", "probe": {"found": False}})
check("A19 关闭时一律 auto（行为与 v3.4.x 一致）", off["action"] == ACTION_AUTO)
sc_bad = ConfidenceScorer({"mode": "不存在的模式"})
check("A20 非法模式回落到 shadow（安全默认）", sc_bad.mode == "shadow")

sc_streak = ConfidenceScorer({"max_low_conf_streak": 3})
sc_streak.note_low(True)
sc_streak.note_low(True)
hit = sc_streak.note_low(True)
check("A21 连续低置信达到阈值才提醒", hit is True and sc_streak.low_streak == 3)
sc_streak.note_low(False)
check("A22 一旦正常即清零", sc_streak.low_streak == 0)
sm = sc.summary()
check("A23 运行汇总可用（含量化分级）",
      sm and sm["count"] == len(sc.history) and "buckets" in sm, "n=%d" % sm["count"])
check("A24 短摘要格式可读", "置信" in ConfidenceScorer.short(best) and "/5" in ConfidenceScorer.short(best))


# ======================================================================
# B. 环境守卫
# ======================================================================
section("B. 环境守卫 core/env_guard.py")


class FakePage:
    """假浏览器：只提供环境守卫需要的两个接口。"""

    def __init__(self, url="", deep=None, boom=False):
        self._url = url
        self._deep = deep or {}
        self._boom = boom

    def current_url(self):
        if self._boom:
            raise RuntimeError("websocket closed")
        return self._url

    def send_cmd(self, method, params=None):
        return {"result": {"value": self._deep}}


g = EnvironmentGuard()
check("B1 正常业务页 → ok", g.check(FakePage("https://app.x.com/home"))["kind"] == "ok")
check("B2 登录页命中 → login", g.check(FakePage("https://app.x.com/login"))["kind"] == "login")
check("B3 错误页命中 → error_page", g.check(FakePage("https://app.x.com/500"))["kind"] == "error_page")
check("B4 空地址 → cdp_down", g.check(FakePage(""))["kind"] == "cdp_down")
check("B5 读取异常 → cdp_down（不抛出）", g.check(FakePage(boom=True))["kind"] == "cdp_down")
check("B6 未接入浏览器 → 跳过而非误判", g.check(None)["kind"] == "ok" and g.check(None)["skipped"])

dlg = g.check(FakePage("https://app.x.com/home",
                       {"url": "https://app.x.com/home", "title": "系统", "dialog": "请选择期间",
                        "password_input": False}), deep=True)
check("B7 深检发现弹窗 → dialog", dlg["kind"] == "dialog" and "请选择期间" in dlg["detail"])

# 关键防误报：正常停留在含 /login 的展示页但**没有密码框**时不应判为会话过期
no_pw = g.check(FakePage("https://app.x.com/login",
                         {"url": "https://app.x.com/login", "title": "功能介绍",
                          "dialog": "", "password_input": False}), deep=True)
check("B8 深检无密码框时 /login 不误判（防误报）", no_pw["kind"] == "ok")

g_off = EnvironmentGuard({"enabled": False})
check("B9 关闭后一律 ok（可整体旁路）", g_off.check(FakePage("https://x.com/500"))["kind"] == "ok")

rec_login = g.recover(FakePage("https://app.x.com/login"), "login")
check("B10 会话过期诚实返回「需人工登录」而不假装能自愈",
      rec_login["ok"] is False and "人工" in rec_login["action"])
rec_unknown = g.recover(FakePage("https://app.x.com/home"), "ok")
check("B11 无异常时不乱动作", rec_unknown["ok"] is False and rec_unknown["action"] == "none")


# ======================================================================
# C. 步骤战绩
# ======================================================================
section("C. 步骤战绩 core/step_stats.py")

s1 = {"type": "browser", "func": "click_elem", "args": ["#a"], "element_ref": "el_1",
      "id": "act_1", "ts": 1, "status": "pending"}
s2 = dict(s1, id="act_999", ts=888888, status="done", comment="变了")
check("C1 指纹忽略易变字段（id/ts/status 变化不影响）",
      step_fingerprint(s1) == step_fingerprint(s2))
check("C2 参数不同则指纹不同",
      step_fingerprint(s1) != step_fingerprint(dict(s1, args=["#b"])))
check("C3 元素引用不同则指纹不同",
      step_fingerprint(s1) != step_fingerprint(dict(s1, element_ref="el_2")))

st = StepStats()
st.record(s1, True)
check("C4 样本不足时不给判断（防止一次失败就被永久打标签）",
      st.success_rate(s1) is None)
st.record(s1, True)
check("C5 样本足够后返回真实成功率", st.success_rate(s1) == 1.0)
st.record(s1, False, err="元素不见了")
check("C6 混入失败后成功率下降", abs(st.success_rate(s1) - 0.6667) < 0.01,
      "%.4f" % st.success_rate(s1))
check("C7 未记录过的步骤返回 None", st.success_rate({"type": "x", "func": "y"}) is None)
ss = st.summary()
check("C8 汇总能指出最差步骤", ss["worst"] and ss["worst"][0]["fail"] == 1,
      "rate=%.2f" % ss["worst"][0]["rate"])

stats_path = os.path.join(tempfile.gettempdir(), "apc_stats_v350.json")
st.save(stats_path)
st_reload = StepStats.load(stats_path)
check("C9 战绩可持久化并在下次运行复用", st_reload.success_rate(s1) == st.success_rate(s1))
try:
    os.remove(stats_path)
except Exception:
    pass


# ======================================================================
# D. 检查点与回滚
# ======================================================================
section("D. 检查点 core/checkpoint.py")

ck = CheckpointStore(keep=3)
ck.record_ok(1, url="https://x.com/1")
ck.record_ok(2, url="https://x.com/2")
check("D1 取最近一个已知良好状态", ck.last_good()["url"] == "https://x.com/2")
ck.record_ok(3, app="wps.exe")   # 桌面步骤：没有 URL
check("D2 need_url 会跳过桌面检查点（回滚只对浏览器有意义）",
      ck.last_good(need_url=True)["idx"] == 2)
ck.record_ok(4, url="https://x.com/4")
ck.record_ok(5, url="https://x.com/5")
check("D3 环形上限生效（不无限增长）", len(ck.items) == 3 and ck.items[0]["idx"] == 3)
ck.record_ok(5, url="https://x.com/5-retry")
check("D4 同一步重试成功后覆盖而非追加",
      len([i for i in ck.items if i["idx"] == 5]) == 1)
check("D5 before_idx 可限定回滚范围",
      ck.last_good(before_idx=5, need_url=True)["idx"] == 4)


# ======================================================================
# E. 主循环集成
# ======================================================================
section("E. 主循环集成（预探测复用 / 策略轮换 / 自适应介入）")

import main_task as mt

ELEM_ID = "el_login"
ELEMENT = {
    "id": ELEM_ID, "domain": "x.com", "name": "登录按钮", "kind": "web",
    "locators": [
        {"strategy": "id", "query": "#good", "hit": 0, "miss": 0, "miss_streak": 0, "stale": False},
        {"strategy": "name", "query": "#alt", "hit": 0, "miss": 0, "miss_streak": 0, "stale": False},
    ],
    "hit_count": 0, "miss_streak": 0, "stale": False,
}
ELEMENT_MISS = {
    "id": "el_miss", "domain": "x.com", "name": "找不到的按钮", "kind": "web",
    "locators": [{"strategy": "id", "query": "#nope", "hit": 0, "miss": 0,
                  "miss_streak": 0, "stale": False}],
    "hit_count": 0, "miss_streak": 0, "stale": False,
}


class FakeBrowser:
    """最小可用的假 CDP 引擎。resolve_locator 支持 avoid（策略轮换）。"""

    def __init__(self, url="https://x.com/home", fail_click=False, deep=None):
        self._url = url
        self.fail_click = fail_click
        self._deep = deep or {}
        self.calls = []
        self.resolve_calls = []
        self.last_resolve = {}
        self.probe_map = {"#good": 1, "#alt": 1}

    # --- CDP 接口 ---
    def connect(self):
        self.calls.append("connect")

    def current_url(self):
        return self._url

    def send_cmd(self, method, params=None):
        self.calls.append("cmd:%s" % method)
        # 深检（弹窗/密码框）走 Runtime.evaluate，按需返回预设特征
        if self._deep and method == "Runtime.evaluate":
            return {"result": {"value": self._deep}}
        return {"result": {"value": None}}

    def _probe(self, sel):
        c = self.probe_map.get(sel, 0)
        return {"found": c > 0, "count": c, "x": 10, "y": 10}

    def resolve_locator(self, element, priority=None, record=True, avoid=None):
        avoid = set(avoid or ())
        self.resolve_calls.append({"id": element.get("id"), "avoid": sorted(avoid)})
        for loc in element.get("locators", []):
            if loc["strategy"] in avoid:
                continue
            if self.probe_map.get(loc["query"]):
                self.last_resolve = {"query": loc["query"], "strategy": loc["strategy"],
                                     "found": True, "count": 1}
                return loc["query"]
        self.last_resolve = {"query": None, "strategy": None, "found": False, "count": 0}
        return None

    # --- 动作 ---
    def open_url(self, u):
        self._url = u
        self.calls.append("open_url:%s" % u)

    def click_elem(self, sel):
        self.calls.append("click:%s" % sel)
        if self.fail_click:
            raise RuntimeError("按钮被遮挡，点击无效")

    def input_text(self, sel, text):
        self.calls.append("input:%s" % sel)

    def hover(self, sel):
        self.calls.append("hover:%s" % sel)

    def upload_file(self, sel, files):
        self.calls.append("upload:%s" % sel)

    def key_press(self, keys):
        self.calls.append("key:%s" % keys)

    def page_text(self, limit=0):
        return ""


class FakeGui:
    def __getattr__(self, name):
        def _noop(*a, **k):
            return "OK"
        return _noop


def build_runner(browser, steps, elems, mode="shadow", max_retry=2):
    r = mt.BreakPointTaskRunner()
    r.browser = browser
    r.gui = FakeGui()
    r.cfg["max_retry"] = max_retry
    r.cfg["delay_base"] = 0
    r.max_retry = max_retry
    r.scorer.mode = mode
    r.break_data = {"current_step": 0, "total_step": len(steps), "task_status": "stop"}
    for e in elems:
        r.repo.elements[e["id"]] = json.loads(json.dumps(e))
    r.load_task_flow = lambda: steps
    r.bp = []
    r.save_breakpoint = lambda step, status="running", err="": r.bp.append((step, status))
    r._persist = lambda: None
    r._save_repo = lambda: None
    return r


def run_silent(runner):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runner.start_run()
    return buf.getvalue()


# ---- E1：成功路径 + 预探测复用 ----
fb1 = FakeBrowser()
steps1 = [
    {"type": "browser", "func": "open_url", "args": ["https://x.com/home"]},
    {"type": "browser", "func": "click_elem", "args": ["#good"], "element_ref": ELEM_ID},
]
r1 = build_runner(fb1, steps1, [ELEMENT])
out1 = run_silent(r1)
clicks1 = [c for c in fb1.calls if c.startswith("click:")]
check("E1 全流程正常跑完并标记 finish", r1.bp and r1.bp[-1][1] == "finish", str(r1.bp[-1]))
check("E2 预探测结果被复用（一步只定位一次，不为评分多花一次 CDP 往返）",
      len([c for c in fb1.resolve_calls if c["id"] == ELEM_ID]) == 1,
      "resolve 次数=%d" % len([c for c in fb1.resolve_calls if c["id"] == ELEM_ID]))
check("E3 点击用了元素库选出的稳定定位器", clicks1 == ["click:#good"], str(clicks1))
check("E4 结束时汇报置信度统计", "置信度：平均" in out1)
check("E5 shadow 模式声明自己是观察模式（不打断）", "观察模式" in out1)

# ---- E2：策略轮换（核心：重试要换做法，不是原样重跑） ----
fb2 = FakeBrowser(fail_click=True)
r2 = build_runner(fb2, list(steps1), [ELEMENT], max_retry=3)
out2 = run_silent(r2)
clicks2 = [c for c in fb2.calls if c.startswith("click:")]
check("E6 失败重试时切换了定位策略（不是原样重跑）",
      len(clicks2) >= 2 and clicks2[0] != clicks2[1], str(clicks2))
check("E7 轮换说明写进了现场输出", "换一种定位方式重试" in out2)
check("E8 环境正常时第二轮换成「环境修复」（重载页面）",
      any(c == "cmd:Page.reload" for c in fb2.calls),
      str([c for c in fb2.calls if c.startswith("cmd:")]))

# 环境明显异常（被踢回登录页）时应当**优先修环境**——在错误页面上换定位器毫无意义
fb_hi = FakeBrowser(url="https://x.com/login", fail_click=True,
                    deep={"url": "https://x.com/login", "title": "登录",
                          "dialog": "", "password_input": True})
# 注意：流程里不能先放 open_url，否则地址已被改写、登录页状态就看不到了
steps_hi = [{"type": "browser", "func": "click_elem", "args": ["#good"],
             "element_ref": ELEM_ID}]
r_hi = build_runner(fb_hi, steps_hi, [ELEMENT], max_retry=2)
out_hi = run_silent(r_hi)
check("E8a 环境异常时优先修环境（不在登录页上瞎换定位器）",
      "换一种定位方式重试" not in out_hi and "环境" in out_hi)
check("E8b 会话过期时如实告知需人工登录（不假装自愈）",
      "人工" in out_hi)
check("E9 全部失败后断点记为 error（可续跑）",
      any(s == "error" for _, s in r2.bp), str(r2.bp[-1]))
check("E10 失败诊断区分「环境问题」与「元素问题」",
      "元素定位或业务前置条件" in out2 or "环境诊断" in out2)
check("E11 失败时也写入了战绩（供下次置信度参考）",
      r2.stats.samples(steps1[1]) >= 1, "samples=%d" % r2.stats.samples(steps1[1]))

# ---- E3：自适应介入 ----
asked = []


def make_ask(choice):
    def _ask(message, **kw):
        asked.append({"headline": kw.get("headline") or "",
                      "options": [o["value"] for o in kw.get("options") or []],
                      "message": message})
        return choice
    return _ask


fb3 = FakeBrowser()
steps3 = [{"type": "browser", "func": "click_elem", "args": ["#nope"],
           "element_ref": "el_miss"}]
r3 = build_runner(fb3, list(steps3), [ELEMENT_MISS], mode="adaptive")
mt.ask_choice = make_ask("continue")
out3 = run_silent(r3)
check("E12 低置信步骤在 adaptive 模式下弹出确认", len(asked) == 1, "asked=%d" % len(asked))
check("E13 确认框给出置信度标题与归因依据",
      asked and "置信" in asked[0]["headline"], asked[0]["headline"] if asked else "")
check("E14 提供继续/跳过/暂停三个选项",
      asked and asked[0]["options"] == ["continue", "skip", "pause"],
      str(asked[0]["options"]) if asked else "")
check("E15 低置信归因出现在现场输出", "元素库" in out3)

asked = []
r4 = build_runner(FakeBrowser(), list(steps3), [ELEMENT_MISS], mode="adaptive")
mt.ask_choice = make_ask("skip")
out4 = run_silent(r4)
check("E16 用户选择跳过 → 该步不执行且流程继续",
      "已按你的选择跳过该步" in out4 and r4.bp[-1][1] == "finish", str(r4.bp[-1]))

asked = []
r5 = build_runner(FakeBrowser(), list(steps3), [ELEMENT_MISS], mode="adaptive")
mt.ask_choice = make_ask("pause")
out5 = run_silent(r5)
check("E17 用户选择暂停 → 断点记为 paused（可原样续跑）",
      any(s == "paused" for _, s in r5.bp), str(r5.bp[-1]))

asked = []
r6 = build_runner(FakeBrowser(), list(steps3), [ELEMENT_MISS], mode="shadow")
mt.ask_choice = make_ask("pause")
out6 = run_silent(r6)
check("E18 shadow 模式下绝不弹窗（零打扰观察期）", len(asked) == 0)
check("E19 shadow 模式仍然把低置信标出来", "置信" in out6)

mt.ask_choice = lambda *a, **k: "continue"

# ---- E4：极低置信直接不执行 ----
asked = []
fb7 = FakeBrowser(url="")   # 空 URL → cdp_down → 极低置信
r7 = build_runner(fb7, [{"type": "browser", "func": "click_elem", "args": ["#good"],
                         "element_ref": ELEM_ID}], [ELEMENT], mode="adaptive")
mt.ask_choice = make_ask("pause")
out7 = run_silent(r7)
check("E20 环境通道断开时置信度触底", "极低" in out7 or "置信 0" in out7 or "置信 0." in out7)
check("E21 极低置信时只给「跳过/暂停」，不提供「继续执行」",
      not asked or asked[0]["options"] == ["skip", "pause"],
      str(asked[0]["options"]) if asked else "（未弹窗，已被环境封顶）")
mt.ask_choice = lambda *a, **k: "continue"

# ---- E5：置信度关闭时行为与旧版一致 ----
fb8 = FakeBrowser()
r8 = build_runner(fb8, list(steps1), [ELEMENT])
r8.scorer.enabled = False
out8 = run_silent(r8)
check("E9b 关闭置信度后不再评分（零回归风险）",
      "置信度：平均" not in out8 and r8.bp[-1][1] == "finish")


# ======================================================================
section("汇总")
print("  通过 %d 项，失败 %d 项" % (PASS[0], FAIL[0]))
if FAIL[0]:
    print("  ❌ 有失败项，请检查上方 FAIL 行")
    sys.exit(1)
print("  ✅ 全部通过")
sys.exit(0)
