# -*- coding: utf-8 -*-
"""v3.4.1 两项机制离线验证（临时脚本，验证通过后删除）。

覆盖：
  A. 元素库健康度：命中/未命中计数、定位器级自动遗忘、元素级自动遗忘、复活、reset、报告、落盘
  B. resolve_locator：跳过已遗忘定位器、全遗忘时兜底、命中/未命中记账
  C. Verifier：11 种校验类型、数组语义、浏览器缺失时跳过、超时轮询
  D. actions schema：verify 字段序列化往返 + comment 追加校验摘要
  E. main_task 集成：校验失败抛错并归因 miss、T1 成功但校验不过 → 自动降级 T2
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("OK  " if cond else "FAIL", name, ("  | " + str(extra)) if extra else ""))


# ============================================================
# A. 元素库健康度 + 自动遗忘
# ============================================================
from core.element_repo import ElementRepository

print("\n=== A. 元素库健康度与自动遗忘 ===")
repo = ElementRepository(stale_threshold=3, locator_stale_threshold=2)
eid = repo.register("baidu.com", "搜索框", [
    {"strategy": "placeholder", "query": "[placeholder='搜索']"},
    {"strategy": "css_chain", "query": "div>input.bad"},
])
el = repo.get(eid)
check("A1 注册后健康字段就位",
      el["hit_count"] == 0 and el["miss_streak"] == 0 and el["stale"] is False)

check("A2 首次命中不计为复活", repo.record_hit(eid, "placeholder") is False)
check("A3 命中累加与定位器记账",
      el["hit_count"] == 1 and el["locators"][0]["hit"] == 1 and el["last_ok_ts"])

r1 = repo.record_miss(eid, "css_chain")
check("A4 第 1 次定位器未命中未遗忘", r1["locator_stale_now"] is False
      and el["locators"][1]["stale"] is False)
r2 = repo.record_miss(eid, "css_chain")
check("A5 定位器连续 2 次未命中 → 自动遗忘该策略",
      r2["locator_stale_now"] is True and el["locators"][1]["stale"] is True)

check("A6 best_locator 跳过已遗忘策略",
      repo.best_locator(eid, ["css_chain", "placeholder"])["strategy"] == "placeholder")

repo.record_miss(eid, "css_chain")
check("A7 元素连续 3 次未命中 → 标记待复核（stale）",
      el["stale"] is True and el["miss_streak"] == 3 and "自动遗忘" in el["stale_reason"])

check("A8 再次命中 → 元素复活且计数归零", repo.record_hit(eid, "placeholder") is True
      and el["stale"] is False and el["miss_streak"] == 0)

for _ in range(2):
    repo.record_miss(eid, "css_chain")
best = repo.best_locator(eid, ["css_chain", "placeholder"])
check("A9 复活后再次遗忘仍能避开（跨轮次稳定）", best["strategy"] == "placeholder")

eid2 = repo.register("x.com", "单策略按钮", [{"strategy": "css", "query": "#b"}])
for _ in range(2):
    repo.record_miss(eid2, "css")
b2 = repo.best_locator(eid2, ["css"])
check("A10 全部策略已遗忘时兜底返回（不直接失败）", bool(b2) and b2.get("all_stale") is True)

repo.record_miss(eid2, "css")     # 第 3 次 → 达元素级阈值，标记待复核
hr = repo.health_report()
check("A11 健康度报告", hr["total"] == 2 and hr["stale"] == 1 and hr["healthy"] == 1,
      "stale=%d weakest=%d" % (hr["stale"], len(hr["weakest"])))

tmp_json = os.path.join(tempfile.gettempdir(), "_apc_health_test.json")
repo.save(tmp_json, skip_presets=True)
repo_reload = ElementRepository.load(tmp_json)
check("A12 落盘/读取往返含健康字段与阈值",
      repo_reload.get(eid)["hit_count"] >= 1 and repo_reload.stale_threshold == 3
      and repo_reload.get(eid)["locators"][0]["hit"] >= 1)

n_reset = repo.reset_health(only_stale=True)
check("A13 人工复核恢复被遗忘元素", n_reset >= 1 and repo.health_report()["stale"] == 0)

# 兼容旧元素库（无健康字段）
legacy = {"version": 1, "elements": [
    {"id": "el_old", "domain": "a.com", "name": "旧元素", "kind": "web",
     "locators": [{"strategy": "css", "query": "#old"}], "stability": 4.5,
     "note": "", "used": 3}]}
legacy_path = os.path.join(tempfile.gettempdir(), "_apc_legacy_test.json")
with open(legacy_path, "w", encoding="utf-8") as f:
    import json as _json
    _json.dump(legacy, f, ensure_ascii=False)
repo_legacy = ElementRepository.load(legacy_path)
check("A14 旧版元素库自动补齐健康字段（向后兼容）",
      repo_legacy.get("el_old")["hit_count"] == 0
      and repo_legacy.get("el_old")["locators"][0]["stale"] is False)

# ============================================================
# B. resolve_locator 跳过已遗忘定位器
# ============================================================
print("\n=== B. resolve_locator 与健康度联动 ===")
from cdp_engine import CdpBrowserCtrl


class FakeCdp(CdpBrowserCtrl):
    def __init__(self, exists_map):
        super().__init__(port=9222)
        self.exists_map = dict(exists_map)
        self.probed = []

    def _probe(self, selector):
        self.probed.append(selector)
        n = self.exists_map.get(selector, 0)
        return {"count": n, "x": 1, "y": 1, "found": n > 0}


elem = {
    "id": "el_t", "domain": "t.com", "name": "测试", "kind": "web",
    "locators": [
        # id 策略在前（优先级更高）但元素已不存在 → 应被记账并最终遗忘
        {"strategy": "id", "query": "#missing", "hit": 0, "miss": 0,
         "miss_streak": 0, "stale": False},
        {"strategy": "placeholder", "query": "#good", "hit": 0, "miss": 0,
         "miss_streak": 0, "stale": False},
    ],
    "used": 0,
}
cdp = FakeCdp({"#good": 1})          # 只有 #good 存在
sel = cdp.resolve_locator(elem)
check("B1 首选策略未命中后回退到次选策略并命中", sel == "#good"
      and elem["locators"][1]["hit"] == 1, "probed=%s" % cdp.probed)
check("B2 未命中策略记 miss", elem["locators"][0]["miss"] >= 1,
      "miss=%d" % elem["locators"][0]["miss"])

for _ in range(4):
    cdp.resolve_locator(elem)
check("B3 连续未命中后脆弱策略被自动遗忘",
      elem["locators"][0]["stale"] is True,
      "miss_streak=%d" % elem["locators"][0]["miss_streak"])

cdp.probed = []
cdp.exists_map = {"#missing": 1, "#good": 1}   # 界面改回来了：首选策略其实可用
cdp.resolve_locator(elem)
check("B4 已遗忘策略不再被探测（省时且不再干扰）",
      "#missing" not in cdp.probed and "#good" in cdp.probed, "probed=%s" % cdp.probed)

elem["locators"][0]["stale"] = True
elem["locators"][1]["stale"] = True
cdp.probed = []
cdp.exists_map = {"#missing": 1}
sel = cdp.resolve_locator(elem)
check("B5 全部遗忘时走兜底轮次，仍能定位成功", sel == "#missing", "probed=%s" % cdp.probed)

# ============================================================
# C. Verifier 校验器
# ============================================================
print("\n=== C. Verifier 校验器 ===")
from core.verify import Verifier


class FakeBrowser:
    def element_count(self, s):
        return {"#a": 1, "#b": 0, ".list li": 3}.get(s, 0)

    def page_text(self):
        return "订单列表 保存成功 共 3 条"

    def current_url(self):
        return "https://x.com/orders/list/"

    def element_value(self, s):
        return "已提交成功"


vf = FakeBrowser()
v = Verifier(browser=vf, timeout=0.4, interval=0.1)

cases = [
    ({"type": "element_exists", "selector": "#a"}, True),
    ({"type": "element_exists", "selector": "#b"}, False),
    ({"type": "element_absent", "selector": "#b"}, True),
    ({"type": "element_count", "selector": ".list li", "count": 3}, True),
    ({"type": "element_count", "selector": ".list li", "count": 5}, False),
    ({"type": "text_present", "text": "保存成功"}, True),
    ({"type": "text_present", "text": "保存失败"}, False),
    ({"type": "text_absent", "text": "保存失败"}, True),
    ({"type": "url_contains", "value": "orders"}, True),
    ({"type": "url_contains", "value": "customers"}, False),
    ({"type": "url_equals", "value": "https://x.com/orders/list"}, True),
    ({"type": "element_value_contains", "selector": "#v", "value": "提交"}, True),
    ({"type": "element_value_equals", "selector": "#v", "value": "未提交"}, False),
    ({"type": "window_title", "value": "不存在的窗口标题xyz"}, False),
    ({"type": "file_exists", "path": __file__}, True),
    ({"type": "file_exists", "path": __file__ + ".nope"}, False),
    ({"type": "unknown_thing"}, False),
]
bad = []
for spec, expect in cases:
    got = v.check(spec)["ok"]
    if got != expect:
        bad.append((spec.get("type"), expect, got))
check("C1 十七种校验用例全部符合预期", not bad, "不符=%s" % bad)

check("C2 数组语义：任一不过则整体不过",
      v.check([{"type": "element_exists", "selector": "#a"},
               {"type": "text_present", "text": "不存在文本"}])["ok"] is False
      and v.check([{"type": "element_exists", "selector": "#a"},
                   {"type": "text_present", "text": "保存成功"}])["ok"] is True)

r_skip = Verifier(browser=None, timeout=0.2).check({"type": "element_exists", "selector": "#a"})
check("C3 浏览器未连接时跳过而非误判失败", r_skip["ok"] is True and r_skip["skipped"] is True)

check("C4 空校验规格视为通过（不拦流程）", Verifier(browser=vf).check(None)["ok"] is True)

# ============================================================
# D. actions schema
# ============================================================
print("\n=== D. actions verify 字段 ===")
from core.actions import Action, actions_to_taskflow

act = Action("browser", "click_elem", ["#btn"], element_ref="el_x",
             verify=[{"type": "text_present", "text": "保存成功"},
                     {"type": "element_absent", "selector": ".loading"}])
rt = Action.from_dict(act.to_dict())
check("D1 verify 序列化/反序列化往返一致", rt.verify == act.verify)
steps = actions_to_taskflow([act])
check("D2 导出 task_flow 带 verify 字段", steps[0].get("verify") == act.verify)
check("D3 comment 追加校验摘要",
      "校验" in steps[0]["comment"] and "保存成功" in steps[0]["comment"],
      steps[0]["comment"])
no_v = actions_to_taskflow([Action("browser", "click_elem", ["#btn"])])
check("D4 无校验的步骤不加后缀", "校验" not in no_v[0]["comment"])

# ============================================================
# E. main_task 集成
# ============================================================
print("\n=== E. main_task 集成（校验失败归因 + T1 降级）===")
from main_task import BreakPointTaskRunner

runner = BreakPointTaskRunner()
runner.browser = vf                       # 用假浏览器，避免真连 CDP
runner.verifier.timeout = 0.3
runner.verifier.interval = 0.1

step_ok = {"type": "browser", "func": "click_elem", "args": ["#a"],
           "verify": [{"type": "text_present", "text": "保存成功"}]}
res = runner._verify(Action.from_dict(step_ok), step_ok)
check("E1 校验通过时返回结果", bool(res) and res["ok"] is True)

step_bad = {"type": "browser", "func": "click_elem", "args": ["#a"],
            "verify": [{"type": "text_present", "text": "绝不可能出现的文本"}]}
raised = ""
try:
    runner._verify(Action.from_dict(step_bad), step_bad)
except RuntimeError as e:
    raised = str(e)
check("E2 校验不通过抛错（不再默认成功）", "结果校验未通过" in raised, raised[:70])

eid_t = runner.repo.register("t.com", "被测按钮", [{"strategy": "css", "query": "#a"}])
step_bad2 = {"type": "browser", "func": "click_elem", "args": ["#a"], "element_ref": eid_t,
             "verify": [{"type": "text_present", "text": "绝不可能出现的文本"}]}
try:
    runner._verify(Action.from_dict(step_bad2), step_bad2)
except RuntimeError:
    pass
check("E3 校验失败计入元素库 miss（定位器归因）",
      runner.repo.get(eid_t)["miss_streak"] == 1)

# T1 成功但界面无变化 → 自动降级 T2
runner.api_registry.register("GET_probe", {"method": "GET", "base_url": "http://x",
                                           "path": "/probe"})
runner.tier_resolver.api_registry = runner.api_registry.templates
calls = []
runner._run_t1 = lambda a: calls.append("T1")
runner._run_browser = lambda a: calls.append("T2")

step_t1_ok = {"type": "browser", "func": "click_elem", "args": ["#a"],
              "t1_ref": {"type": "api", "name": "GET_probe"}}
runner.run_single_step(step_t1_ok)
check("E4 T1 有路径且校验通过 → 只跑 T1", calls == ["T1"], calls)

calls = []
step_t1_verify_fail = {"type": "browser", "func": "click_elem", "args": ["#a"],
                       "t1_ref": {"type": "api", "name": "GET_probe"},
                       "verify": [{"type": "text_present", "text": "绝不可能出现的文本"}]}
raised5 = ""
try:
    runner.run_single_step(step_t1_verify_fail)
except RuntimeError as e:
    raised5 = str(e)
check("E5 T1 接口成功但界面没变化 → 自动降级 T2 且界面校验仍不过则整步失败",
      calls == ["T1", "T2"] and "结果校验未通过" in raised5,
      "calls=%s raised=%s" % (calls, raised5[:40]))

calls = []


def _boom(a):
    calls.append("T1")
    raise RuntimeError("T1 接口挂了")


runner._run_t1 = _boom
step_t1_fail = {"type": "browser", "func": "click_elem", "args": ["#a"],
                "t1_ref": {"type": "api", "name": "GET_probe"}}
runner.run_single_step(step_t1_fail)
check("E6 T1 抛错 → 仍按原逻辑降级 T2", calls == ["T1", "T2"], calls)

# ============================================================
print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
