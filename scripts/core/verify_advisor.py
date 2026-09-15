# -*- coding: utf-8 -*-
"""录制时自动推断 verify 候选（v3.6.0）

## 为什么需要它

v3.4.1 给回放加了「步骤级结果校验」（见 core/verify.py），但校验规格要人手写。
实测痛点：录 60 步的流程，能正确手写出 verify 的通常不到 10 步——因为要回想
「我点这个按钮之后，页面到底发生了什么变化」，这恰恰是录制时最容易拿到、
事后最想不起来的信息。

录制器天然知道答案：它知道点之前页面长什么样、点之后变成了什么样。所以让
录制器顺手把「我观测到的变化」写成校验规格，人只做复核，比手写省力得多。

## 设计红线（为什么不做「猜」）

本模块**只断言录制时真实观测到的状态**，绝不推测：

  - URL 变了 → 才写 url_contains（没变就不写，不猜「应该会跳转」）
  - 字段回读值等于录入文本 → 才写 element_value_equals
  - 反馈文本在动作前不存在、动作后出现 → 才写 text_present

因此生成出来的校验项在回放时几乎不可能「因为规格本身写错」而误报。
这是刻意的取舍：自动生成的校验宁少勿错——**一个误报的校验比没有校验更糟**，
因为它会把本来成功的流程打断（触发重试 → 降级 → 最终失败）。

## 两条过滤

1. **易变文本过滤**：含数字/日期/金额的反馈文案（如「订单 12345 保存成功」）
   第二次运行必然不同，直接丢弃，并在报告里说明原因。
2. **敏感字段过滤**：密码框、验证码、身份证/卡号类输入不回读值，避免把凭证
   写进 task_flow.json 的校验规格。

## 分级

  high   —— 直接观测到的事实（URL 跳转、回读值一致、反馈文案出现），默认采用
  medium —— 合理但有一点点不确定性（字段被页面规范化、弹窗/加载遮罩、窗口标题），
            需要显式开 all 才写入

## 用法

    from core.verify_advisor import VerifyAdvisor
    adv = VerifyAdvisor(mode="auto")
    report = adv.attach(steps, events)      # events 含动作事件与 probe 事件
    print(adv.render_report("task_flow.json"))

支持三种模式：
    off    什么都不挂（等同 v3.5.0 行为）
    auto   只挂 high（默认）
    all    挂 high + medium
"""

import re
from urllib.parse import urlsplit

# ---- 分级常量 ----
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"

MODES = ("off", "auto", "all")

# ---- 规则名（用于报告）----
RULE_URL = "url_change"
RULE_VALUE = "input_readback"
RULE_FEEDBACK = "feedback_text"
RULE_DIALOG = "dialog_appeared"
RULE_LOADING = "loading_finished"
RULE_WINDOW = "window_focus"
RULE_MANUAL = "manual_kept"

RULE_LABEL = {
    RULE_URL: "URL 变化",
    RULE_VALUE: "录入回读",
    RULE_FEEDBACK: "反馈文案",
    RULE_DIALOG: "弹窗出现",
    RULE_LOADING: "加载结束",
    RULE_WINDOW: "窗口焦点",
    RULE_MANUAL: "手工校验",
}

# 可能因导航 / 弹窗 / 反馈而值得校验的网页动作
_URL_FUNCS = ("click_elem", "key_press", "upload_file", "press_keys")
_FEEDBACK_FUNCS = ("click_elem", "key_press", "upload_file", "press_keys")
_VALUE_FUNCS = ("input_text",)
# 桌面动作（浏览器侧规则不适用）
_GUI_FUNCS = ("click_at", "double_click_at", "right_click_at", "hover_at",
              "drag_move", "press_keys", "open_software")

# 敏感字段：不回读值，避免凭证落进校验规格
_SENSITIVE_SEL = re.compile(
    r"password|passwd|passcode|pwd|secret|token|otp|captcha|"
    r"验证码|密码|身份证|idcard|id_card|bankcard|card_no|cardno|cvv",
    re.I)
_LONG_DIGITS = re.compile(r"^\d{11,}$")     # 手机号 / 身份证 / 卡号
_DIGITS = re.compile(r"\d")
_UUIDISH = re.compile(r"^[0-9a-fA-F]{8,}$")
_PURE_NUM = re.compile(r"^\d+$")
_TIME_WORDS = ("年", "月", "日", "时", "分", "秒", "上午", "下午", "刚刚")
_NOISE_HINTS = ("Copyright", "©", "隐私政策", "服务协议", "ICP")

MAX_TEXT_LEN = 24          # 反馈文案长度上限（超过基本是正文而非提示）
_MIN_TEXT_LEN = 2
_PROBE_WINDOW_MS = 4000    # 动作事件与其 probe 的最大时间差
_FOCUS_WINDOW_MS = 3000    # 桌面动作与焦点事件的最大时间差
_URL_LOOKAHEAD_MS = 6000   # 在动作后多久之内找「新 URL」


def _norm(s):
    """文本归一化（折叠空白），用于比较。"""
    return " ".join(str(s or "").split())


# ---------------------------------------------------------------------------
# 文本 / URL 工具
# ---------------------------------------------------------------------------

def is_stable_text(t):
    """判断一段反馈文案是否「每次运行都一样」。

    含数字、日期、金额的文案（「订单 12345 保存成功」「2026-09-15 已同步」）
    第二次跑必然不同，不能当断言。这是自动生成校验最常见的误报来源。
    """
    s = _norm(t)
    if len(s) < _MIN_TEXT_LEN or len(s) > MAX_TEXT_LEN:
        return False
    if _DIGITS.search(s):
        return False
    if any(w in s for w in _TIME_WORDS):
        return False
    if any(n in s for n in _NOISE_HINTS):
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", s):
        return False
    return True


def looks_sensitive(selector, value):
    """密码/验证码/证件号类字段不回读值。"""
    if selector and _SENSITIVE_SEL.search(str(selector)):
        return True
    v = str(value or "").strip()
    if v and _LONG_DIGITS.match(v):
        return True
    return False


def _split_url(url):
    """返回 URL 的路径分段；非 http(s) 或解析失败返回 None。"""
    try:
        s = urlsplit(str(url or ""))
    except Exception:
        return None
    if s.scheme not in ("http", "https"):
        return None
    return [x for x in (s.path or "/").split("/") if x]


def _looks_volatile_seg(seg):
    """路径段是否易变（ID / 时间戳 / 长哈希）——易变段不能当锚点。"""
    if not seg:
        return True
    if _PURE_NUM.match(seg):
        return True
    bare = seg.replace("-", "").replace("_", "")
    if len(bare) >= 8 and _UUIDISH.match(bare):
        return True
    if len(seg) > 32:
        return True
    return False


def _stable_tail(segs):
    """从尾部剥掉易变段。"""
    out = list(segs)
    while out and _looks_volatile_seg(out[-1]):
        out.pop()
    return out


def url_verify(pre_url, post_url):
    """由「动作前 URL → 动作后 URL」推断 url_contains。

    返回 (spec, why) 或 (None, 跳过原因)。

    只取**新增的路径段**并锚定上一段作为上下文，例如
    /order/list → /order/list/detail/88  得到  url_contains: /order/list/detail
    （末尾的 88 是易变 ID，丢掉）。

    仅查询串变化（?t=123）不生成——查询串里的值通常带时间戳/随机数。
    """
    pre = _split_url(pre_url)
    post = _split_url(post_url)
    if pre is None or post is None:
        return None, "URL 不是 http/https 或为空"
    if pre == post:
        return None, "路径未变化"
    n = 0
    while n < len(pre) and n < len(post) and pre[n] == post[n]:
        n += 1
    start = n - 1 if n >= 1 else 0        # 带上一个共同段，增强辨识度
    segs = _stable_tail(post[start:])
    if not segs:
        segs = _stable_tail(post[n:])
    if not segs:
        return None, "变化全部发生在易变段（ID/时间戳）上"
    val = "/" + "/".join(segs)
    if len(val) < 3:
        return None, "可锚定的路径段太短"
    why = "动作前后 URL 路径由 /%s 变为 /%s" % ("/".join(pre), "/".join(post))
    return {"type": "url_contains", "value": val}, why


def window_title_value(focus_ev):
    """从桌面焦点事件推断 window_title 的断言值（优先应用名，其次标题首段）。"""
    if not focus_ev:
        return None
    app = _norm(focus_ev.get("app"))
    if app and app != "未知应用" and is_stable_text(app):
        return app
    title = _norm(focus_ev.get("title"))
    if not title:
        return None
    for sep in (" - ", " — ", " – ", " | ", "-", "—"):
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    if is_stable_text(title):
        return title
    return None


# ---------------------------------------------------------------------------
# 候选结构
# ---------------------------------------------------------------------------

def _cand(rule, specs, level, why):
    """specs 统一成 list（反馈文案可能一次命中两条）。"""
    if isinstance(specs, dict):
        specs = [specs]
    return {"rule": rule, "specs": list(specs), "level": level, "why": why}


class _StepPlan:
    """一个步骤的推断结果。"""

    def __init__(self, ts, idx=0):
        self.ts = ts
        self.idx = idx
        self.desc = ""          # 步骤人读描述（报告用）
        self.step = None        # 对应的步骤 dict（挂载用）
        self.candidates = []
        self.skipped = []       # [(rule, 跳过原因)]
        self.applied = False    # 是否真的写进了 step["verify"]

    def add(self, cand):
        self.candidates.append(cand)

    def skip(self, rule, why):
        self.skipped.append((rule, why))

    def accepted(self, mode):
        """按模式筛出可采用的候选。"""
        if mode == "off":
            return []
        allow = (LEVEL_HIGH,) if mode == "auto" else (LEVEL_HIGH, LEVEL_MEDIUM)
        return [c for c in self.candidates if c["level"] in allow]

    def specs(self, mode):
        out = []
        for c in self.accepted(mode):
            out.extend(c["specs"])
        return out


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class VerifyAdvisor:
    """从录制事件推断 verify 候选，并把结果挂到步骤上。

    mode: "off" / "auto"（默认，只挂 high）/ "all"（high + medium）
    """

    def __init__(self, mode="auto", max_per_step=3, max_feedback=2):
        self.mode = mode if mode in MODES else "auto"
        self.max_per_step = max_per_step
        self.max_feedback = max_feedback
        self.report = {}

    # ---------- 事件整理 ----------

    @staticmethod
    def split_events(events):
        """把事件流拆成 动作事件 / probe 探测 / 桌面焦点 三类。"""
        actions, probes, focuses = [], [], []
        for ev in events or []:
            t = ev.get("type")
            if t == "probe":
                probes.append(ev)
            elif t == "focus":
                focuses.append(ev)
            else:
                actions.append(ev)
        return actions, probes, focuses

    @staticmethod
    def probes_for(ts, probes):
        """取某个动作对应的全部 probe（按时间差限制窗口），按探测延时排序。"""
        if not ts:
            return []
        out = [p for p in probes
               if p.get("for_ts") == ts and abs((p.get("ts") or 0) - ts) <= _PROBE_WINDOW_MS]
        return sorted(out, key=lambda p: p.get("delay", 0) or 0)

    @staticmethod
    def _url_after(ts, actions, probes):
        """动作之后观测到的第一个 URL（navigate 事件或 probe 都可能带来）。"""
        if not ts:
            return None
        best = None
        for ev in list(actions) + list(probes):
            t = ev.get("ts") or 0
            if t <= ts or t - ts > _URL_LOOKAHEAD_MS:
                continue
            url = ev.get("url")
            if not url:
                continue
            if best is None or t < best[0]:
                best = (t, url)
        return best[1] if best else None

    # ---------- 单步推断 ----------

    def infer_step(self, step, ev, actions, probes, focuses, idx=0):
        """推断一个步骤的 verify 候选，返回 _StepPlan。"""
        ts = ev.get("ts") or step.get("ts")
        plan = _StepPlan(ts, idx)
        if self.mode == "off":
            return plan

        func = step.get("func", "")
        args = step.get("args") or []
        selector = args[0] if args else ""
        kind = step.get("type", "")

        if func == "open_url":
            plan.skip(RULE_URL, "打开网页这一步本身就是导航，不需要校验")
            return plan

        step_probes = self.probes_for(ts, probes)
        pre = ev.get("pre")

        # ---- 规则 1：URL 变化 ----
        if func in _URL_FUNCS and kind == "browser":
            post_url = self._url_after(ts, actions, probes)
            pre_url = ev.get("url")
            if not post_url:
                plan.skip(RULE_URL, "动作之后没有再观测到页面（可能直接关闭标签页）")
            elif post_url == pre_url:
                plan.skip(RULE_URL, "URL 未变化（原地刷新的流程不写 URL 校验）")
            else:
                spec, why = url_verify(pre_url, post_url)
                if spec:
                    plan.add(_cand(RULE_URL, spec, LEVEL_HIGH, why))
                else:
                    plan.skip(RULE_URL, why)

        # ---- 规则 2：录入值回读（仅网页：桌面录入没有选择器可回读）----
        if func in _VALUE_FUNCS and kind == "browser":
            typed = args[1] if len(args) > 1 else ""
            self._infer_value(plan, selector, typed, step_probes)

        # ---- 规则 3/4/5：反馈文案 / 弹窗 / 加载结束 ----
        if func in _FEEDBACK_FUNCS and kind == "browser":
            if pre is None:
                plan.skip(RULE_FEEDBACK, "本次录制没有「动作前快照」，无法判断文案是否为新出现")
                plan.skip(RULE_DIALOG, "本次录制没有「动作前快照」")
                plan.skip(RULE_LOADING, "本次录制没有「动作前快照」")
            else:
                self._infer_feedback(plan, pre, step_probes)
                self._infer_dialog(plan, pre, step_probes)
                self._infer_loading(plan, pre, step_probes)

        # ---- 规则 6：桌面窗口标题 ----
        if func in _GUI_FUNCS:
            self._infer_window(plan, ts, focuses)

        return plan

    def _infer_value(self, plan, selector, typed, step_probes):
        """录入后回读字段值 —— 抓住「输入没进去」这个最常见失败。"""
        typed = "" if typed is None else str(typed)
        if not selector or not typed.strip():
            plan.skip(RULE_VALUE, "没有可校验的录入内容")
            return
        if looks_sensitive(selector, typed):
            plan.skip(RULE_VALUE, "疑似密码/验证码/证件号字段，跳过以免凭证写入校验规格")
            return

        probed = None
        acted_exists = None
        for p in step_probes:
            if p.get("acted_value") is not None:
                probed = p.get("acted_value")
                acted_exists = p.get("acted_exists")
                break

        if probed is not None and acted_exists is False:
            plan.skip(RULE_VALUE, "录入后元素已消失（页面已跳转），无法回读")
            return

        if probed is None:
            plan.add(_cand(RULE_VALUE,
                           {"type": "element_value_equals", "selector": selector, "value": typed},
                           LEVEL_MEDIUM,
                           "未观测到回填值，按录入文本断言（若页面会自动格式化可能误判）"))
            return

        probed_s = "" if probed is None else str(probed)
        if probed_s == typed:
            plan.add(_cand(RULE_VALUE,
                           {"type": "element_value_equals", "selector": selector, "value": typed},
                           LEVEL_HIGH,
                           "录制时回读确认字段值确实等于录入文本「%s」" % typed))
        elif typed and typed in probed_s:
            plan.add(_cand(RULE_VALUE,
                           {"type": "element_value_contains", "selector": selector, "value": typed},
                           LEVEL_HIGH,
                           "录制时回读值为「%s」，包含录入文本" % probed_s[:40]))
        elif not probed_s.strip():
            plan.skip(RULE_VALUE,
                      "录入后字段为空，页面没接受这个输入——应先修流程而不是加校验")
        else:
            plan.add(_cand(RULE_VALUE,
                           {"type": "element_value_equals", "selector": selector,
                            "value": probed_s},
                           LEVEL_MEDIUM,
                           "页面把录入文本规范化成「%s」，按录制时观测到的值断言" % probed_s[:40]))

    def _collect_post(self, step_probes, key):
        """把各次探测里的某一类观测聚起来（保留首次出现顺序）。"""
        out = []
        for p in step_probes:
            for item in ((p.get("post") or {}).get(key) or []):
                if isinstance(item, dict):
                    out.append(item)
        return out

    def _infer_feedback(self, plan, pre, step_probes):
        """反馈文案（toast/alert）：动作前不存在、动作后出现了才算本次反馈。"""
        pre_texts = {_norm(x.get("text")) for x in (pre.get("feedback") or [])
                     if isinstance(x, dict)}
        found, volatile = [], []
        for item in self._collect_post(step_probes, "feedback"):
            t = _norm(item.get("text"))
            if not t or t in pre_texts:
                continue
            if not is_stable_text(t):
                if t not in volatile:
                    volatile.append(t)
                continue
            if t not in found:
                found.append(t)
        if not found:
            if volatile:
                plan.skip(RULE_FEEDBACK,
                          "反馈文案含数字/日期等易变内容，跳过：%s"
                          % "、".join(volatile[:3]))
            return
        # 去掉互为子串的短文案，保留信息量更大的那条
        picked = []
        for t in sorted(found, key=len, reverse=True):
            if any(t in other for other in picked):
                continue
            picked.append(t)
            if len(picked) >= self.max_feedback:
                break
        specs = [{"type": "text_present", "text": t} for t in picked]
        plan.add(_cand(RULE_FEEDBACK, specs, LEVEL_HIGH,
                       "录制时观测到操作后出现提示「%s」（操作前不存在）" % "」「".join(picked)))

    def _infer_dialog(self, plan, pre, step_probes):
        """弹窗出现 → element_exists。"""
        pre_sels = {_norm(x.get("selector")) for x in (pre.get("dialogs") or [])
                    if isinstance(x, dict)}
        for item in self._collect_post(step_probes, "dialogs"):
            sel = _norm(item.get("selector"))
            if not sel or sel in pre_sels:
                continue
            txt = _norm(item.get("text"))[:20]
            plan.add(_cand(RULE_DIALOG,
                           {"type": "element_exists", "selector": sel},
                           LEVEL_MEDIUM,
                           "录制时观测到操作后弹出对话框%s" % ("「%s」" % txt if txt else "")))
            return

    def _infer_loading(self, plan, pre, step_probes):
        """动作前在加载、之后消失 → element_absent（顺带等加载结束）。"""
        pre_sels = [x.get("selector") for x in (pre.get("loading") or [])
                    if isinstance(x, dict) and x.get("selector")]
        if not pre_sels:
            return
        for p in step_probes:
            post = p.get("post") or {}
            if post.get("loading"):
                continue                        # 还在转，等下一次探测
            plan.add(_cand(RULE_LOADING,
                           {"type": "element_absent", "selector": pre_sels[0]},
                           LEVEL_MEDIUM,
                           "动作前页面在加载（%s），录制中观测到已结束" % pre_sels[0]))
            return

    def _infer_window(self, plan, ts, focuses):
        """桌面动作 → window_title（顺带确认「点的是对的那个窗口」）。"""
        if not ts:
            plan.skip(RULE_WINDOW, "没有时间戳，无法关联焦点事件")
            return
        near = None
        for f in focuses:
            d = abs((f.get("ts") or 0) - ts)
            if d <= _FOCUS_WINDOW_MS and (near is None or d < near[0]):
                near = (d, f)
        if not near:
            plan.skip(RULE_WINDOW, "没有观测到相邻的窗口焦点事件")
            return
        val = window_title_value(near[1])
        if not val:
            plan.skip(RULE_WINDOW, "窗口标题含数字或过长，不稳定，跳过")
            return
        plan.add(_cand(RULE_WINDOW,
                       {"type": "window_title", "value": val},
                       LEVEL_MEDIUM,
                       "录制时该操作发生在窗口「%s」内" % val))

    # ---------- 批量挂载 ----------

    def attach(self, steps, events, force=False):
        """把推断出的 verify 挂到 steps 上，返回报告 dict。

        steps 与 events 通过 **时间戳** 对齐（录制器会给每一步写 ts）。
        force=False 时，步骤上已有的 verify 一律保留不动（尊重手工编辑）。
        """
        actions, probes, focuses = self.split_events(events)
        by_ts = {ev["ts"]: ev for ev in actions if ev.get("ts")}

        plans = []
        for i, step in enumerate(steps or []):
            if not isinstance(step, dict):
                continue
            ts = step.get("ts")
            ev = by_ts.get(ts)
            if ev is None:
                # 没有原始事件（例如手工写的步骤）→ 合成最小事件，仍能推断 URL/窗口
                ev = {"type": "synthetic", "ts": ts, "url": step.get("_url")}
            plan = self.infer_step(step, ev, actions, probes, focuses, idx=i)
            plan.step = step
            plan.desc = (step.get("comment")
                         or "%s(%s)" % (step.get("func"), step.get("args")))
            plans.append(plan)

        applied = 0
        for plan in plans:
            step = plan.step
            if plan.accepted(self.mode) == []:
                continue
            if step.get("verify") and not force:
                plan.skipped.append((RULE_MANUAL, "该步已有手工写的 verify，保留不动"))
                continue
            specs = plan.specs(self.mode)[:self.max_per_step]
            step["verify"] = specs[0] if len(specs) == 1 else specs
            plan.applied = True
            applied += 1

        self.report = {
            "mode": self.mode,
            "steps": len(plans),
            "applied": applied,
            "candidates": sum(len(p.candidates) for p in plans),
            "plans": plans,
            "applied_plans": [p for p in plans if p.applied],
            "skipped_plans": [p for p in plans if p.skipped],
        }
        return self.report

    # ---------- 报告 ----------

    def render_report(self, task_flow_path=None):
        """生成人读报告（Markdown），供用户复核后删改。"""
        r = self.report or {}
        mode = r.get("mode", self.mode)
        lines = ["# 自动生成的校验候选（verify）", ""]
        lines += [
            "> 本文件由录制器生成，**只记录录制时真实观测到的变化**，不做任何猜测。",
            "> 已按下面的模式写进 `task_flow.json` 的 `verify` 字段；不需要的直接删。",
            "> 想全部关掉：录制时加 `--verify off`。",
            "",
            "## 一、本次结果",
            "",
            "| 项目 | 值 |",
            "| --- | --- |",
            "| 模式 | `%s` |" % mode,
            "| 步骤总数 | %d |" % r.get("steps", 0),
            "| 推断出候选 | %d 条 |" % r.get("candidates", 0),
            "| 实际写入 | **%d 步** |" % r.get("applied", 0),
            "| 流程文件 | `%s` |" % (task_flow_path or "-"),
            "",
        ]
        if mode == "auto":
            lines += ["模式 `auto` 只写入**高置信**校验（录制时直接观测到的事实：",
                      "URL 跳转、录入值回读一致、反馈文案出现）。想让弹窗 / 加载遮罩 /",
                      "窗口标题这类校验也生效，改用 `--verify all`。", ""]
        elif mode == "all":
            lines += ["模式 `all` 写入高置信 + 中置信校验。中置信项（弹窗、加载遮罩、",
                      "窗口标题、录入值被页面规范化）偶尔可能误报 —— **回放莫名失败时先怀疑它们**。", ""]
        elif mode == "off":
            lines += ["模式 `off`：没有生成任何校验（等同 v3.5.0 行为）。", ""]

        applied = r.get("applied_plans") or []
        if applied:
            lines += ["## 二、已写入的校验", ""]
            for plan in applied:
                lines.append("### 第 %d 步 · %s" % (plan.idx + 1, plan.desc or plan.ts))
                lines.append("")
                lines.append("| 校验类型 | 内容 | 置信 | 依据 |")
                lines.append("| --- | --- | --- | --- |")
                for c in plan.accepted(mode):
                    for spec in c["specs"]:
                        lines.append("| `%s` | %s | %s | %s |" % (
                            spec.get("type", ""), _brief_value(spec), c["level"], c["why"]))
                lines.append("")

        skipped = r.get("skipped_plans") or []
        if skipped:
            lines += ["## 三、跳过的地方（及原因）", "",
                      "| 步骤 | 规则 | 原因 |", "| --- | --- | --- |"]
            for plan in skipped:
                for rule, why in plan.skipped:
                    lines.append("| 第 %d 步 | %s | %s |"
                                 % (plan.idx + 1, RULE_LABEL.get(rule, rule), why))
            lines.append("")

        lines += [
            "## 四、复核建议",
            "",
            "1. **先跑一遍**。自动生成的校验宁少勿错，但业务语义仍需你确认 ——",
            "   比如提示文案换了措辞、或某一步其实允许两种结果。",
            "2. **删掉不需要的**：编辑 `task_flow.json` 该步的 `verify` 字段即可；",
            "   数组里删掉某一项，不用整个删。",
            "3. **误报先查易变内容**：回放报「页面应包含文本 X」失败，多半是 X 里带了",
            "   单号/日期。改成 `element_exists`（等某个元素出现）更稳。",
            "4. **敏感字段永远不会被回读**：密码、验证码、身份证/卡号类输入一律",
            "   不生成校验，避免凭证写进流程文件。",
            "",
        ]
        return "\n".join(lines)


def _brief_value(spec):
    """把 spec 缩成报告表格里那一列的可读内容。"""
    t = spec.get("type", "")
    if t in ("text_present", "text_absent"):
        return "「%s」" % spec.get("text", "")
    if t in ("url_contains", "url_equals"):
        return "`%s`" % spec.get("value", "")
    if t in ("element_value_equals", "element_value_contains"):
        return "`%s` = 「%s」" % (spec.get("selector", ""), spec.get("value", ""))
    if t in ("element_exists", "element_absent", "element_count"):
        return "`%s`" % spec.get("selector", "")
    if t == "window_title":
        return "「%s」" % spec.get("value", "")
    return "`%s`" % t


def build_from_config(cfg, mode=None):
    """从 config.json 构造（录制器另走 CLI 参数，这里供回放侧/测试使用）。"""
    c = (cfg or {}).get("verify_advisor") or {}
    return VerifyAdvisor(mode=mode or c.get("mode", "auto"))


if __name__ == "__main__":
    # 自检：合成事件，验证核心规则
    evs = [
        {"type": "navigate", "initial": True, "url": "http://x.com/order/list", "ts": 1000},
        {"type": "click", "url": "http://x.com/order/list", "ts": 2000,
         "selector": "#new", "pre": {"feedback": [], "dialogs": [], "loading": []}},
        {"type": "probe", "for_ts": 2000, "delay": 700, "ts": 2700,
         "url": "http://x.com/order/detail/88",
         "post": {"feedback": [], "dialogs": [], "loading": []}},
        {"type": "change", "url": "http://x.com/order/detail/88", "ts": 3000,
         "selector": "#cust", "value": "C001", "pre": {}},
        {"type": "probe", "for_ts": 3000, "delay": 700, "ts": 3700,
         "url": "http://x.com/order/detail/88", "acted_value": "C001", "acted_exists": True,
         "post": {"feedback": [{"text": "保存成功", "selector": ".toast"}],
                  "dialogs": [], "loading": []}},
    ]
    steps = [
        {"type": "browser", "func": "open_url", "args": ["http://x.com/order/list"], "ts": 1000},
        {"type": "browser", "func": "click_elem", "args": ["#new"], "ts": 2000,
         "comment": "点击【新建】"},
        {"type": "browser", "func": "input_text", "args": ["#cust", "C001"], "ts": 3000,
         "comment": "录入「C001」→【客户编码】"},
    ]
    adv = VerifyAdvisor(mode="all")
    rep = adv.attach(steps, evs)
    for s in steps:
        print(s["func"], "->", s.get("verify"))
    print("-" * 60)
    print(rep["candidates"], "candidates,", rep["applied"], "applied")
    assert steps[0].get("verify") is None, "open_url 不该有 verify"
    assert steps[1]["verify"] == {"type": "url_contains", "value": "/order/detail"}
    assert steps[2]["verify"] == {"type": "element_value_equals",
                                 "selector": "#cust", "value": "C001"}
    print(adv.render_report("task_flow.json")[:200])
    print("self-test OK")
