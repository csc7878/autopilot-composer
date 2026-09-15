# -*- coding: utf-8 -*-
"""v3.6.0 离线验证：录制时自动生成 verify 候选（core/verify_advisor.py + 三个录制器接线）。

全程合成事件，不需要浏览器 / 不需要 GPU / 不需要真实录制。

分组：
  A. 文本与 URL 工具函数（易变文本、易变路径段、URL 差分）
  B. URL 变化规则
  C. 录入回读规则（含敏感字段过滤）
  D. 反馈文案规则（含易变文案过滤）
  E. 弹窗 / 加载遮罩规则
  F. 桌面窗口标题规则
  G. 模式与挂载行为（off / auto / all、不覆盖手工 verify、open_url 免疫、裁剪）
  H. 录制器接线（events_to_taskflow 带 ts、probe 不进流程、playwright 导出忽略 probe）
  I. 端到端（合成一次完整录制会话，跑通 infer → attach → 报告）
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.verify_advisor import (  # noqa: E402
    VerifyAdvisor, is_stable_text, looks_sensitive, url_verify, window_title_value,
    LEVEL_HIGH, LEVEL_MEDIUM, RULE_URL, RULE_VALUE, RULE_FEEDBACK, RULE_DIALOG,
    RULE_LOADING, RULE_WINDOW, RULE_MANUAL,
)
from core.verify import SUPPORTED_TYPES as VERIFY_TYPES  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s%s" % (name, ("   [%s]" % extra) if extra else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("   [%s]" % extra) if extra else ""))


def sec(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


# ---------------------------------------------------------------------------
# 构造事件的辅助函数
# ---------------------------------------------------------------------------
BASE_URL = "http://erp.local/order/list"


def pre_snap(feedback=None, dialogs=None, loading=None):
    return {"feedback": feedback or [], "dialogs": dialogs or [], "loading": loading or []}


def click_ev(ts, selector="#btn", url=BASE_URL, pre=None):
    return {"type": "click", "ts": ts, "selector": selector, "url": url,
            "pre": pre if pre is not None else pre_snap()}


def probe_ev(for_ts, ts, url=BASE_URL, feedback=None, dialogs=None, loading=None,
             acted_value=None, acted_exists=None, delay=700):
    p = {"type": "probe", "for_ts": for_ts, "ts": ts, "delay": delay, "url": url,
         "post": pre_snap(feedback, dialogs, loading)}
    if acted_value is not None:
        p["acted_value"] = acted_value
    if acted_exists is not None:
        p["acted_exists"] = acted_exists
    return p


def change_ev(ts, selector, value, url=BASE_URL, pre=None):
    return {"type": "change", "ts": ts, "selector": selector, "value": value, "url": url,
            "pre": pre if pre is not None else pre_snap()}


def step(func, args, ts, **kw):
    d = {"type": kw.pop("kind", "browser"), "func": func, "args": list(args), "ts": ts,
         "comment": kw.pop("comment", "")}
    d.update(kw)
    return d


# ===========================================================================
sec("A. 文本 / URL 工具函数")
# ===========================================================================
ok("A1 正常中文短句稳定（保存成功）", is_stable_text("保存成功") is True)
ok("A2 含数字视为易变（订单 12345 已保存）", is_stable_text("订单 12345 已保存") is False)
ok("A3 含日期字视为易变（今日 已同步）", is_stable_text("今日 已同步") is False)
ok("A4 纯英文状态稳定（Saved）", is_stable_text("Saved") is True)
ok("A5 过短（单字）不稳定", is_stable_text("好") is False)
ok("A6 过长（>24）不稳定",
   is_stable_text("这是一段特别长的提示文案用来测试长度上限是否真的生效") is False)
ok("A7 页脚版权信息被排除", is_stable_text("Copyright") is False)
ok("A8 纯标点不稳定", is_stable_text("——") is False)

ok("A9 密码字段识别为敏感", looks_sensitive("#userPassword", "abc123") is True)
ok("A10 验证码字段识别为敏感", looks_sensitive("#captcha", "8a2b") is True)
ok("A11 长数字值识别为敏感（手机号）", looks_sensitive("#mobile", "13800138000") is True)
ok("A12 普通字段不敏感", looks_sensitive("#customerCode", "C001") is False)

spec, why = url_verify("/order/list", "/order/list")
ok("A13 路径未变化 → 不生成", spec is None, why)
spec, _ = url_verify("http://x/a/b", "http://x/a/b/")
ok("A14 末尾斜杠不算变化", spec is None)
spec, why = url_verify(BASE_URL, "http://erp.local/order/detail/88")
ok("A15 锚定新增段并剥掉 ID", spec == {"type": "url_contains", "value": "/order/detail"},
   str(spec))
spec, why = url_verify(BASE_URL, "http://erp.local/login")
ok("A16 跨目录跳转锚定新段", spec == {"type": "url_contains", "value": "/login"}, str(spec))
spec, why = url_verify(BASE_URL, BASE_URL + "?t=1700000000")
ok("A17 仅查询串变化不生成本校验（易变）", spec is None, why)
spec, why = url_verify(BASE_URL, "http://erp.local/order/list")
ok("A18 同一路径不再生成", spec is None)
spec, why = url_verify("about:blank", "http://x/a")
ok("A19 非 http 起始地址被拒", spec is None, why)
spec, why = url_verify("http://x/a", "http://x/a/8f3c1d9e2b7a4c5f")
ok("A20 变化段全为哈希/ID → 拒绝", spec is None, why)
ok("A21 窗口标题优先取应用名",
   window_title_value({"app": "企业微信", "title": "企业微信 - 张三"}) == "企业微信")
ok("A22 无应用名时取标题首段",
   window_title_value({"app": "", "title": "WPS 表格 - 预算表"}) == "WPS 表格")
ok("A23 标题含数字且无可切分 → 拒绝",
   window_title_value({"app": "", "title": "报表2026"}) is None)


# ===========================================================================
sec("B. URL 变化规则")
# ===========================================================================
evs = [click_ev(2000), probe_ev(2000, 2700, url="http://erp.local/order/detail/88")]
steps = [step("click_elem", ["#btn"], 2000, comment="点击【新建订单】")]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("B1 点击后 URL 变化 → 生成 url_contains",
   steps[0].get("verify") == {"type": "url_contains", "value": "/order/detail"},
   str(steps[0].get("verify")))
ok("B2 该候选为高置信", any(c["level"] == LEVEL_HIGH and c["rule"] == RULE_URL
                             for c in adv.report["plans"][0].candidates))

evs = [click_ev(2000), probe_ev(2000, 2700)]
steps = [step("click_elem", ["#btn"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("B3 URL 未变化 → 不生成", steps[0].get("verify") is None)

evs = [click_ev(2000), probe_ev(2000, 2700), probe_ev(2000, 3800)]
steps = [step("click_elem", ["#btn"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("B4 两次探测都不变 → 仍不生成", steps[0].get("verify") is None)

evs = [click_ev(2000), probe_ev(2000, 2700, url=BASE_URL + "?t=1")]
steps = [step("click_elem", ["#btn"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("B5 仅查询串变化 → 不生成", steps[0].get("verify") is None)

evs = [{"type": "keys", "ts": 2000, "keys": ["Enter"], "url": BASE_URL, "pre": pre_snap()},
       probe_ev(2000, 2700, url="http://erp.local/order/result")]
steps = [step("key_press", [["Enter"]], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("B6 按键引发跳转 → 也生成 url_contains",
   steps[0].get("verify") == {"type": "url_contains", "value": "/order/result"})


# ===========================================================================
sec("C. 录入回读规则")
# ===========================================================================
evs = [change_ev(2000, "#cust", "C001"), probe_ev(2000, 2700, acted_value="C001",
                                                   acted_exists=True)]
steps = [step("input_text", ["#cust", "C001"], 2000, comment="录入「C001」→【客户编码】")]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("C1 回读值一致 → element_value_equals 且为高置信",
   steps[0].get("verify") == {"type": "element_value_equals", "selector": "#cust",
                             "value": "C001"},
   str(steps[0].get("verify")))

evs = [change_ev(2000, "#cust", "C001")]
steps = [step("input_text", ["#cust", "C001"], 2000)]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("C1b 没有探测数据时不生成高置信项（降级为中置信，auto 不写入）",
   steps[0].get("verify") is None)
steps = [step("input_text", ["#cust", "C001"], 2000)]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("C1c all 模式下未回读也写入（中置信）",
   steps[0].get("verify", {}).get("type") == "element_value_equals")

evs = [change_ev(2000, "#amount", "1000"), probe_ev(2000, 2700, acted_value="¥1000.00",
                                                    acted_exists=True)]
steps = [step("input_text", ["#amount", "1000"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("C2 回读值包含录入文本 → element_value_contains（高置信）",
   steps[0].get("verify") == {"type": "element_value_contains", "selector": "#amount",
                             "value": "1000"},
   str(steps[0].get("verify")))

evs = [change_ev(2000, "#amount", "1000"), probe_ev(2000, 2700, acted_value="1,000.00",
                                                    acted_exists=True)]
steps = [step("input_text", ["#amount", "1000"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("C3 页面规范化为不含原文 → auto 不写入（中置信）", steps[0].get("verify") is None)
steps = [step("input_text", ["#amount", "1000"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("C4 all 模式按观测值断言",
   steps[0].get("verify") == {"type": "element_value_equals", "selector": "#amount",
                             "value": "1,000.00"},
   str(steps[0].get("verify")))

evs = [change_ev(2000, "#pwd", "secret123"), probe_ev(2000, 2700, acted_value="secret123",
                                                      acted_exists=True)]
steps = [step("input_text", ["#pwd", "secret123"], 2000)]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("C5 密码字段不生成校验（避免凭证落盘）", steps[0].get("verify") is None)
ok("C5b 报告里说明了跳过原因",
   any("敏感" in why or "凭证" in why for _, why in adv.report["plans"][0].skipped))

evs = [change_ev(2000, "#mobile", "13800138000"),
       probe_ev(2000, 2700, acted_value="13800138000", acted_exists=True)]
steps = [step("input_text", ["#mobile", "13800138000"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("C6 手机号（11 位纯数字）不生成校验", steps[0].get("verify") is None)

evs = [change_ev(2000, "#cust", "C001"), probe_ev(2000, 2700, acted_value="",
                                                  acted_exists=True)]
steps = [step("input_text", ["#cust", "C001"], 2000)]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("C7 录入后字段为空 → 不生成，且提示应先修流程",
   steps[0].get("verify") is None
   and any("没接受" in why for _, why in adv.report["plans"][0].skipped))

evs = [change_ev(2000, "#cust", "C001"), probe_ev(2000, 2700, acted_value="C001",
                                                  acted_exists=False)]
steps = [step("input_text", ["#cust", "C001"], 2000)]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("C8 录入后元素消失 → 不生成", steps[0].get("verify") is None)

evs = [change_ev(2000, "#note", ""), probe_ev(2000, 2700, acted_value="")]
steps = [step("input_text", ["#note", ""], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("C9 清空输入不生成校验", steps[0].get("verify") is None)

evs = [change_ev(2000, "#cust", "C001"), probe_ev(2000, 2700, acted_value="C001",
                                                  acted_exists=True)]
steps = [step("input_text", ["#cust", "C001"], 2000, kind="gui")]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("C10 桌面录入（无选择器可回读）不生成值校验", steps[0].get("verify") is None)


# ===========================================================================
sec("D. 反馈文案规则")
# ===========================================================================
evs = [click_ev(2000),
       probe_ev(2000, 2700, feedback=[{"selector": ".toast", "text": "保存成功"}])]
steps = [step("click_elem", ["#save"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("D1 动作后新出现稳定文案 → text_present（高置信）",
   steps[0].get("verify") == {"type": "text_present", "text": "保存成功"},
   str(steps[0].get("verify")))

evs = [click_ev(2000, pre=pre_snap(feedback=[{"selector": ".toast", "text": "保存成功"}])),
       probe_ev(2000, 2700, feedback=[{"selector": ".toast", "text": "保存成功"}])]
steps = [step("click_elem", ["#save"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("D2 动作前就存在的文案不当作本次反馈", steps[0].get("verify") is None)

evs = [click_ev(2000),
       probe_ev(2000, 2700, feedback=[{"selector": ".toast", "text": "订单 12345 已保存"}])]
steps = [step("click_elem", ["#save"], 2000)]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("D3 含数字的反馈文案被过滤（否则下次必误报）", steps[0].get("verify") is None)
ok("D3b 报告里说明过滤原因",
   any("易变" in why for _, why in adv.report["plans"][0].skipped))

evs = [click_ev(2000),
       probe_ev(2000, 2700, feedback=[{"selector": ".a", "text": "保存成功"},
                                      {"selector": ".b", "text": "保存"}])]
steps = [step("click_elem", ["#save"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
v = steps[0].get("verify")
ok("D4 互为子串的文案只保留信息量更大的那条",
   isinstance(v, dict) and v.get("text") == "保存成功", str(v))

evs = [click_ev(2000),
       probe_ev(2000, 2700, feedback=[{"selector": ".a", "text": "保存成功"},
                                      {"selector": ".b", "text": "已提交审批"}])]
steps = [step("click_elem", ["#save"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
v = steps[0].get("verify")
ok("D5 两条互不包含的文案 → 生成数组（全部通过才算过）",
   isinstance(v, list) and len(v) == 2, str(v))

evs = [click_ev(2000), probe_ev(2000, 2700, feedback=None, url=BASE_URL)]
evs[1]["post"] = None
steps = [step("click_elem", ["#save"], 2000)]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("D6 探测数据缺失不崩（post=None）", steps[0].get("verify") is None)

evs = [click_ev(2000)]
evs[0]["pre"] = None
evs.append(probe_ev(2000, 2700, feedback=[{"selector": ".toast", "text": "保存成功"}]))
steps = [step("click_elem", ["#save"], 2000)]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("D7 无「动作前快照」时不猜反馈（旧版录制产物安全降级）", steps[0].get("verify") is None)
ok("D7b 报告给出跳过原因",
   any("动作前快照" in why for _, why in adv.report["plans"][0].skipped))


# ===========================================================================
sec("E. 弹窗 / 加载遮罩规则")
# ===========================================================================
evs = [click_ev(2000),
       probe_ev(2000, 2700, dialogs=[{"selector": ".ant-modal-content", "text": "确认删除"}])]
steps = [step("click_elem", ["#del"], 2000)]
adv = VerifyAdvisor(mode="auto")
adv.attach(steps, evs)
ok("E1 弹窗为中置信 → auto 不写入", steps[0].get("verify") is None)
steps = [step("click_elem", ["#del"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("E2 all 模式写入 element_exists",
   steps[0].get("verify") == {"type": "element_exists", "selector": ".ant-modal-content"},
   str(steps[0].get("verify")))

evs = [click_ev(2000, pre=pre_snap(dialogs=[{"selector": ".ant-modal-content",
                                            "text": "旧弹窗"}])),
       probe_ev(2000, 2700, dialogs=[{"selector": ".ant-modal-content", "text": "旧弹窗"}])]
steps = [step("click_elem", ["#x"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("E3 动作前已存在的弹窗不算新弹窗", steps[0].get("verify") is None)

evs = [click_ev(2000, pre=pre_snap(loading=[{"selector": ".el-loading-mask"}])),
       probe_ev(2000, 2700, loading=[{"selector": ".el-loading-mask"}]),
       probe_ev(2000, 3800, loading=[], delay=1800)]
steps = [step("click_elem", ["#query"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("E4 加载中 → 加载结束 → element_absent",
   steps[0].get("verify") == {"type": "element_absent", "selector": ".el-loading-mask"},
   str(steps[0].get("verify")))

evs = [click_ev(2000, pre=pre_snap(loading=[{"selector": ".el-loading-mask"}])),
       probe_ev(2000, 2700, loading=[{"selector": ".el-loading-mask"}]),
       probe_ev(2000, 3800, loading=[{"selector": ".el-loading-mask"}], delay=1800)]
steps = [step("click_elem", ["#query"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("E5 一直在加载 → 不生成（不能断言「应消失」）", steps[0].get("verify") is None)

evs = [click_ev(2000), probe_ev(2000, 2700, loading=[{"selector": ".el-loading-mask"}])]
steps = [step("click_elem", ["#query"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("E6 动作前没在加载 → 不生成加载校验", steps[0].get("verify") is None)


# ===========================================================================
sec("F. 桌面窗口标题规则")
# ===========================================================================
evs = [{"type": "focus", "ts": 1900, "app": "企业微信", "exe": "WXWork.exe",
        "title": "企业微信 - 张三"},
       {"type": "click", "ts": 2000, "x": 100, "y": 200, "app": "企业微信",
        "exe": "WXWork.exe"}]
steps = [step("click_at", [100, 200], 2000, kind="gui", comment="点击坐标(100, 200)")]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("F1 桌面点击 → window_title 断言应用名",
   steps[0].get("verify") == {"type": "window_title", "value": "企业微信"},
   str(steps[0].get("verify")))
ok("F2 该项为中置信", adv.report["plans"][0].candidates[0]["level"] == LEVEL_MEDIUM)

steps = [step("click_at", [100, 200], 2000, kind="gui")]
VerifyAdvisor(mode="auto").attach(steps, evs)
ok("F3 auto 模式不写入窗口标题校验", steps[0].get("verify") is None)

evs = [{"type": "focus", "ts": 9000, "app": "微信", "exe": "WeChat.exe", "title": "微信"}]
steps = [step("click_at", [1, 2], 2000, kind="gui")]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("F4 焦点事件太远（>3s）不关联", steps[0].get("verify") is None)

evs = [{"type": "focus", "ts": 2000, "app": "未知应用", "exe": "", "title": "报表2026汇总"}]
steps = [step("click_at", [1, 2], 2000, kind="gui")]
VerifyAdvisor(mode="all").attach(steps, evs)
ok("F5 标题含数字且无法切分 → 不生成", steps[0].get("verify") is None)


# ===========================================================================
sec("G. 模式与挂载行为")
# ===========================================================================
evs = [click_ev(2000), probe_ev(2000, 2700, url="http://erp.local/order/detail/88",
                                feedback=[{"selector": ".t", "text": "保存成功"}])]
steps = [step("click_elem", ["#b"], 2000)]
VerifyAdvisor(mode="off").attach(steps, evs)
ok("G1 off 模式零改动（等价 v3.5.0 行为）", steps[0].get("verify") is None)

steps = [step("click_elem", ["#b"], 2000, verify={"type": "text_present", "text": "手写的"})]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("G2 已有手工 verify 不被覆盖",
   steps[0]["verify"] == {"type": "text_present", "text": "手写的"})
ok("G2b 报告标注「保留不动」",
   any(r == RULE_MANUAL for r, _ in adv.report["plans"][0].skipped))

steps = [step("click_elem", ["#b"], 2000, verify={"type": "text_present", "text": "手写的"})]
VerifyAdvisor(mode="all").attach(steps, evs, force=True)
ok("G3 force=True 时允许覆盖（录制实时挂载用）",
   steps[0]["verify"] != {"type": "text_present", "text": "手写的"})

evs = [{"type": "navigate", "initial": True, "ts": 900, "url": BASE_URL},
       click_ev(2000), probe_ev(2000, 2700, url="http://erp.local/order/detail/88")]
steps = [step("open_url", [BASE_URL], 900), step("click_elem", ["#b"], 2000)]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("G4 open_url 不挂校验（它本身就是导航）", steps[0].get("verify") is None)
ok("G4b 报告说明为何跳过",
   any("打开网页" in why for _, why in adv.report["plans"][0].skipped))
ok("G5 其后步骤正常挂载", steps[1].get("verify") is not None)

evs = [click_ev(2000), probe_ev(2000, 2700, url="http://erp.local/order/detail/88",
                                feedback=[{"selector": ".t", "text": "保存成功"},
                                          {"selector": ".t2", "text": "已同步到云端"}],
                                dialogs=[{"selector": ".m", "text": "提示"}])]
steps = [step("click_elem", ["#b"], 2000)]
VerifyAdvisor(mode="all").attach(steps, evs)
v = steps[0].get("verify")
ok("G6 每步候选有上限（默认最多 3 条）", isinstance(v, list) and len(v) <= 3, str(v))

steps = [step("click_elem", ["#b"], 2000)]
adv = VerifyAdvisor(mode="all", max_per_step=1)
adv.attach(steps, evs)
ok("G7 上限可配置", isinstance(steps[0].get("verify"), dict), str(steps[0].get("verify")))

# 没有 ts 的步骤不应崩，也不该乱挂
steps = [{"type": "browser", "func": "click_elem", "args": ["#x"]}]
adv = VerifyAdvisor(mode="all")
adv.attach(steps, evs)
ok("G8 缺 ts 的老步骤不崩、不乱挂", steps[0].get("verify") is None)

adv = VerifyAdvisor(mode="auto")
adv.attach([], evs)
ok("G9 空步骤列表不崩", adv.report["steps"] == 0)

adv = VerifyAdvisor(mode="whatever")
ok("G10 非法模式回退到 auto", adv.mode == "auto")

r = adv.render_report("task_flow.json")
ok("G11 报告含结果表与复核建议", "本次结果" in r and "复核建议" in r)
ok("G12 报告提到敏感字段不会回读", "敏感" in r)


# ===========================================================================
sec("H. 录制器接线")
# ===========================================================================
import recorder  # noqa: E402
import desktop_recorder as dr  # noqa: E402
import core.actions as A  # noqa: E402

evs = [{"type": "navigate", "initial": True, "ts": 1000, "url": BASE_URL},
       click_ev(2000), probe_ev(2000, 2700, url="http://erp.local/order/detail/88"),
       change_ev(3000, "#cust", "C001"),
       probe_ev(3000, 3700, url="http://erp.local/order/detail/88", acted_value="C001",
                acted_exists=True)]
tf = recorder.events_to_taskflow(evs)
ok("H1 导出的步骤带 ts", all(s.get("ts") for s in tf), str([s.get("ts") for s in tf]))
ok("H2 probe 事件不进流程", all(s.get("func") != "probe" for s in tf))
ok("H3 步骤数正确（navigate+click+change）", len(tf) == 3, str(len(tf)))
ok("H21 桌面事件误入网页转换器不崩（无 selector 时跳过）",
   recorder.events_to_taskflow([{"type": "click", "x": 1, "y": 2, "ts": 9}]) == [])

lines = recorder.events_to_playwright(evs)
ok("H4 Playwright 导出忽略 probe", len(lines) == 3, str(len(lines)))
ok("H5 Playwright 导出无 probe 字样", not any("probe" in l for l in lines))

ok("H6 RECORDER_JS 含动作前快照", "function snapshot()" in recorder.RECORDER_JS)
ok("H7 RECORDER_JS 含动作后探测", "function doProbe" in recorder.RECORDER_JS)
ok("H8 RECORDER_JS 采集反馈容器", "FEEDBACK_SEL" in recorder.RECORDER_JS)
ok("H9 RECORDER_JS 采集弹窗", "DIALOG_SEL" in recorder.RECORDER_JS)
ok("H10 RECORDER_JS 采集加载遮罩", "LOADING_SEL" in recorder.RECORDER_JS)
ok("H11 RECORDER_JS 有扫描上限（防重页面卡顿）", "MAX_SCAN" in recorder.RECORDER_JS)
ok("H12 RECORDER_JS 两次探测", "PROBE_DELAYS = [700, 1800]" in recorder.RECORDER_JS)
ok("H13 探测事件带 for_ts 回指", "for_ts: ts" in recorder.RECORDER_JS)
ok("H14 动作事件带 pre 快照", "obj.pre = snapshot()" in recorder.RECORDER_JS)

ok("H15 _brief_verify 可读化", recorder._brief_verify(
    {"type": "url_contains", "value": "/a/b"}) == "URL 含 /a/b")
ok("H16 _brief_verify 处理数组",
   recorder._brief_verify([{"type": "text_present", "text": "好"},
                          {"type": "url_contains", "value": "/x"}]) == "含文本「好」、URL 含 /x")

ok("H17 桌面导出步骤带 ts",
   all(s.get("ts") for s in dr.events_to_gui_taskflow(
       [{"type": "click", "ts": 5, "x": 1, "y": 2}])))

# Action 也要带 ts（合并会话路径靠它对齐）
a = A.Action("browser", "click_elem", ["#b"], ts=1234)
st = A.actions_to_taskflow([a])
ok("H18 Action 导出为步骤时带 ts", st[0].get("ts") == 1234)
ok("H19 Action 的 verify 能带进步骤",
   A.actions_to_taskflow([A.Action("browser", "click_elem", ["#b"], ts=1,
                                   verify={"type": "text_present", "text": "x"})]
                         )[0].get("verify") == {"type": "text_present", "text": "x"})
ok("H20 SOP 描述会带「校验：」",
   "校验：" in A.describe_action(
       A.Action("browser", "click_elem", ["#b"], ts=1,
                verify={"type": "text_present", "text": "保存成功"})))


# ===========================================================================
sec("I. 端到端：一次完整录制会话")
# ===========================================================================
evs = [
    {"type": "navigate", "initial": True, "ts": 1000, "url": BASE_URL},
    # 1) 点击「新建」→ 跳详情页
    click_ev(2000, "#new", pre=pre_snap()),
    probe_ev(2000, 2700, url="http://erp.local/order/detail/88"),
    # 2) 录入客户编码（回读一致）
    change_ev(3000, "#cust", "C001", url="http://erp.local/order/detail/88"),
    probe_ev(3000, 3700, url="http://erp.local/order/detail/88", acted_value="C001",
             acted_exists=True),
    # 3) 录入手机号（敏感 → 不生成）
    change_ev(3500, "#mobile", "13800138000", url="http://erp.local/order/detail/88"),
    probe_ev(3500, 4200, url="http://erp.local/order/detail/88",
             acted_value="13800138000", acted_exists=True),
    # 4) 点保存 → toast
    click_ev(5000, "#save", url="http://erp.local/order/detail/88", pre=pre_snap()),
    probe_ev(5000, 5700, url="http://erp.local/order/detail/88",
             feedback=[{"selector": ".el-message", "text": "保存成功"}]),
    # 5) 桌面：切到企业微信点一下
    {"type": "focus", "ts": 6000, "app": "企业微信", "exe": "WXWork.exe",
     "title": "企业微信"},
    {"type": "click", "ts": 6100, "x": 300, "y": 400, "app": "企业微信",
     "exe": "WXWork.exe"},
]
web_tf = recorder.events_to_taskflow([e for e in evs if e.get("type") != "focus"])
desk_tf = dr.events_to_gui_taskflow([e for e in evs if e.get("type") in ("focus", "click")
                                     and e.get("x")])
merged = sorted(web_tf + desk_tf, key=lambda s: s.get("ts", 0))

adv = VerifyAdvisor(mode="auto")
adv.attach(merged, evs)
# 立刻统计：merged 与后面 all 模式的列表共享同一批 step dict，
# 再 attach 一次会把中置信项也写进去，所以计数必须在此刻取。
applied_auto = sum(1 for s in merged if s.get("verify"))


def find(func, arg=None):
    for s in merged:
        if s.get("func") == func and (arg is None or (s.get("args") and s["args"][0] == arg)):
            return s
    return {}


ok("I1 打开网页无校验", find("open_url").get("verify") is None)
ok("I2 新建 → url_contains",
   find("click_elem", "#new").get("verify") == {"type": "url_contains",
                                               "value": "/order/detail"},
   str(find("click_elem", "#new").get("verify")))
inputs = [s for s in merged if s.get("func") == "input_text"]
ok("I3 客户编码回读一致 → 已挂校验",
   inputs[0].get("verify") == {"type": "element_value_equals", "selector": "#cust",
                              "value": "C001"}, str(inputs[0].get("verify")))
ok("I4 手机号被敏感过滤 → 无校验", inputs[1].get("verify") is None)
ok("I5 保存按钮无 URL 变化 → 只有文案校验",
   find("click_elem", "#save").get("verify") == {"type": "text_present",
                                                 "text": "保存成功"},
   str(find("click_elem", "#save").get("verify")))
ok("I6 桌面点击在 auto 模式下无校验（窗口标题属中置信）",
   find("click_at").get("verify") is None)
ok("I8 auto 模式实际写入 3 步（跳转/回读/文案）", applied_auto == 3, str(applied_auto))

merged2 = sorted(web_tf + desk_tf, key=lambda s: s.get("ts", 0))
VerifyAdvisor(mode="all").attach(merged2, evs)
c2 = [s for s in merged2 if s.get("func") == "click_at"][0]
ok("I7 all 模式下桌面点击挂上 window_title",
   c2.get("verify") == {"type": "window_title", "value": "企业微信"}, str(c2.get("verify")))

# 报告落盘 + JSON 可序列化
tmpdir = tempfile.mkdtemp(prefix="apc_v360_")
tf_path = os.path.join(tmpdir, "task_flow.json")
with open(tf_path, "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=2)
rep = adv.render_report(tf_path)
md_path = os.path.join(tmpdir, "verify_candidates.md")
with open(md_path, "w", encoding="utf-8") as f:
    f.write(rep)
ok("I9 报告文件已生成", os.path.getsize(md_path) > 500)
ok("I10 报告含步骤级依据", "依据" in rep and "url_contains" in rep)
ok("I11 task_flow.json 可正常读写（verify 可 JSON 序列化）",
   json.load(open(tf_path, encoding="utf-8"))[1].get("verify") is not None)
ok("I12 生成的 verify 全部是受支持类型",
   all(sp.get("type") in VERIFY_TYPES
       for s in merged if s.get("verify")
       for sp in ([s["verify"]] if isinstance(s["verify"], dict) else s["verify"])))

# 回放侧真的能消费这些规格
from core.verify import Verifier, normalize_spec  # noqa: E402
all_specs = []
for s in merged2:
    all_specs.extend(normalize_spec(s.get("verify")))
ok("I13 生成的规格可被 Verifier 解析", len(all_specs) >= 3, str(len(all_specs)))
ok("I14 规格类型全部合法",
   all(sp.get("type") in VERIFY_TYPES for sp in all_specs),
   str([sp.get("type") for sp in all_specs]))
v = Verifier(timeout=0.1, interval=0.05)
res = v.check([sp for sp in all_specs if sp.get("type") in ("text_present", "url_contains")])
ok("I15 浏览器未连接时校园验标记为跳过而非误判失败", res.get("skipped") is True or res["ok"])

import shutil  # noqa: E402
shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
sec("J. 本机回环请求绕过系统代理（真实起服务验证）")
# ===========================================================================
# 背景：本机 HTTP_PROXY 存在时，urllib 会把 127.0.0.1 的 CDP 请求也送进代理，
# 代理返回 502 —— 「Chrome 明明开着却连不上」。本次真机排查时命中该问题。
import threading  # noqa: E402
import urllib.request  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from core import local_http  # noqa: E402

_ph = [h for h in local_http._local_opener.handlers
       if isinstance(h, urllib.request.ProxyHandler)]
# CPython 里 ProxyHandler.__init__ 只对非空 proxies 逐项 setattr 出 `<type>_open`，
# 传 {} 时它不注册任何处理方法，因此根本不会出现在 opener.handlers 里——
# 「找不到 ProxyHandler」正是「该 opener 完全不做代理」的证明。
ok("J1 专用 opener 不注册任何代理处理（ProxyHandler({}) 为空即惰性）",
   _ph == [], str([type(h).__name__ for h in _ph]))
with open(os.path.join(HERE, "core", "local_http.py"), encoding="utf-8") as f:
    ok("J1b local_http 以空 ProxyHandler 构造 opener", "ProxyHandler({})" in f.read())
ok("J2 describe_proxy_env 可用", isinstance(local_http.describe_proxy_env(), str))


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"Browser":"Chrome/151"}')

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), _H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/json/version" % port
try:
    # 用「必然连不上的代理」模拟被劫持
    bad = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": "http://127.0.0.1:1"}))
    proxy_failed = False
    try:
        bad.open(url, timeout=1.5)
    except Exception:
        proxy_failed = True
    ok("J3 对照实验：走代理时本机请求确实失败（复现 502/连接错）", proxy_failed)

    data = json.load(local_http.urlopen_local(url, timeout=3))
    ok("J4 urlopen_local 绕过代理后成功取到 CDP 信息",
       data.get("Browser") == "Chrome/151", str(data))

    # PUT 方法也要能走通（/json/new 用 PUT）
    class _P(_H):
        def do_PUT(self):
            self.do_GET()
    srv.RequestHandlerClass = _P
    d2 = json.load(local_http.urlopen_local(url, timeout=3, method="PUT"))
    ok("J5 urlopen_local 支持 method（CDP /json/new 需要 PUT）", d2.get("Browser") == "Chrome/151")
finally:
    srv.shutdown()

src_checks = [
    ("J6 cdp_engine 使用 urlopen_local", "core.local_http import urlopen_local",
     os.path.join(HERE, "cdp_engine.py")),
    ("J7 recorder 使用 urlopen_local", "_urlopen_local",
     os.path.join(HERE, "recorder.py")),
    ("J8 browser_launcher 使用 urlopen_local", "from core.local_http import urlopen_local",
     os.path.join(HERE, "browser_launcher.py")),
    ("J9 chat_mode 使用 urlopen_local", "from core.local_http import urlopen_local",
     os.path.join(HERE, "chat_mode.py")),
]
for name, needle, path in src_checks:
    with open(path, encoding="utf-8") as f:
        ok(name, needle in f.read())


# ===========================================================================
print("\n" + "=" * 74)
print("汇总")
print("=" * 74)
print("  通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("  " + ("✅ 全部通过" if FAIL == 0 else "❌ 有失败项"))
sys.exit(1 if FAIL else 0)
