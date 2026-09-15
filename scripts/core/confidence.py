# -*- coding: utf-8 -*-
"""置信度评估器（Confidence Scorer）——「确定性版」的自适应人机协作。

理论来源
--------
OS-Kairos（ACL 2025 Findings，arXiv:2503.16465，上海交大 + Meta）指出 GUI 智能体
最核心的可靠性问题是 **过度执行（over-execution）**：智能体在缺乏把握时依然
全自动盲目执行，导致不可逆错误。其解法是让智能体对每一步输出 1~5 的置信度，
低于阈值即请求人类介入（adaptive human-agent collaboration）。

论文实测（复杂场景）：
  - OS-Atlas-Pro-7B 基线：任务成功率 14.29%
  - 纯 prompt 方案请求介入：人类成功率 0%（无效）
  - 「每步都问人」：成功率 62%，但完全不可用（太累）
  - **自适应介入：88.20%**
结论是关键洞察：**「何时介入」比「要不要介入」重要得多**。

本模块的移植思路
----------------
不训练任何模型（本机为 2 核 Pentium / 8GB / 核显 / 32 位 Python，物理上跑不了
VLM）。改为用回放过程中**已经存在**的客观信号合成置信度。相比模型的自我报告
置信度（存在校准偏差、普遍过度自信），确定性信号反而有三个优势：
**可解释、可复现、零算力**。

信号清单（全部确定性、可追溯）：
  + Tier 层级         T1 直调接口远比 T4 屏幕坐标稳
  + 定位器唯一性      唯一命中 > 多匹配 > 未命中
  + 定位器策略秩      id/name/testid > placeholder > css > css_chain > 坐标
  + 元素健康度        历史命中次数 / 连续未命中（element_repo 已统计）
  + 步骤历史成功率    同一指纹步骤过去成功过几次（step_stats）
  + 可验证性          是否配了 verify（配了才不会「假成功」）
  - 环境异常          登录页过期 / 错误页 / 意外弹窗（env_guard）

分级与动作：
  高（>= auto_above）  自动执行
  中（>= confirm_below）自动执行，但失败时优先走策略轮换重试
  低（>= abort_below） 请求人工确认（adaptive 模式）/ 仅告警（shadow 模式）
  极低（< abort_below）直接中止并诊断，不执行（避免不可逆操作）

三种运行模式（config.confidence.mode）：
  off       完全关闭，行为与 v3.4.x 一致
  shadow    默认。只评分、只记录、只告警，**从不阻断**（零风险观察期）
  adaptive  低置信请求人工确认，极低置信直接中止
"""
import json
import os

CONF_MAX = 5.0
CONF_MIN = 0.0

# ---------------- 分级 / 动作常量 ----------------
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"
LEVEL_CRITICAL = "critical"

ACTION_AUTO = "auto"
ACTION_CONFIRM = "confirm"
ACTION_ABORT = "abort"

LEVEL_LABEL = {
    LEVEL_HIGH: "高",
    LEVEL_MEDIUM: "中",
    LEVEL_LOW: "偏低",
    LEVEL_CRITICAL: "极低",
}
LEVEL_ICON = {
    LEVEL_HIGH: "\u2705",      # ✅
    LEVEL_MEDIUM: "\u2714",    # ✔
    LEVEL_LOW: "\u26a0\ufe0f",  # ⚠️
    LEVEL_CRITICAL: "\u26d4",  # ⛔
}

# ---------------- 默认权重 ----------------

# 各 Tier 的基准分：T1 直调几乎不会因界面改版而失效
DEFAULT_TIER_BASE = {
    "T1": 4.7,
    "T2": 4.2,
    "T3": 3.4,
    "T4": 1.8,
}
DEFAULT_TIER_BASE_FALLBACK = 3.0

TIER_LABEL = {
    "T1": "直调接口/命令/数据库",
    "T2": "浏览器元素定位",
    "T3": "桌面元素定位",
    "T4": "屏幕坐标（最脆弱）",
}

# 定位器策略的稳定度加成（对齐 core/locator.py 的 WEB_PRIORITY 顺序）
DEFAULT_STRATEGY_ADJ = {
    "id": 0.35,
    "name": 0.35,
    "testid": 0.25,
    "placeholder": 0.20,
    "aria_label": 0.15,
    "role_name": 0.15,
    "text": 0.10,
    "css": 0.00,
    "xpath": -0.40,
    "css_chain": -0.50,
    "coord": -0.80,
}

# 环境异常时的置信度封顶值（无论其他信号多好，都压到这个上限以下）
ENV_CAP = {
    "cdp_down": 0.5,      # 浏览器通道断了：几乎必然失败
    "dialog": 1.6,        # 有意料之外的弹窗遮挡：可能点错
    "login": 1.2,         # 被踢回登录页：后续步骤全都会在错误页面上空点
    "error_page": 1.0,    # 错误页
    "unknown": 2.0,
}

ENV_LABEL = {
    "cdp_down": "浏览器调试通道不可用",
    "dialog": "出现意料之外的弹窗",
    "login": "被跳转到登录页（会话可能已过期）",
    "error_page": "跳转到了错误页",
    "unknown": "环境状态未知",
}


def parse_tier(tier_desc):
    """从 tier_resolver 的描述（如 'T2(cdp)' / 'T1(call_api)'）提取层级代号。"""
    if not tier_desc:
        return None
    s = str(tier_desc).strip()
    for t in ("T1", "T2", "T3", "T4"):
        if s.startswith(t):
            return t
    return None


class ConfidenceScorer:
    """确定性置信度评分器。

    典型用法（main_task.py）：
        signals = scorer is fed by pre-probe results
        res = scorer.score(step, signals)
        if res["action"] == ACTION_CONFIRM: ...ask human...
    """

    def __init__(self, cfg=None, repo=None, stats=None, env_guard=None):
        c = cfg or {}
        self.enabled = bool(c.get("enabled", True))
        self.mode = str(c.get("mode", "shadow")).lower()
        if self.mode not in ("off", "shadow", "adaptive"):
            self.mode = "shadow"

        self.auto_above = float(c.get("auto_above", 3.5))
        self.confirm_below = float(c.get("confirm_below", 2.5))
        self.abort_below = float(c.get("abort_below", 1.2))
        self.interactive_timeout = int(c.get("interactive_timeout", 60))
        self.max_low_conf_streak = int(c.get("max_low_conf_streak", 3))

        self.tier_base = dict(DEFAULT_TIER_BASE)
        self.tier_base.update(c.get("tier_base") or {})
        self.tier_base.setdefault("_default", DEFAULT_TIER_BASE_FALLBACK)
        self.strategy_adj = dict(DEFAULT_STRATEGY_ADJ)
        self.strategy_adj.update(c.get("strategy_adj") or {})

        self.repo = repo
        self.stats = stats
        self.env_guard = env_guard

        self.history = []          # 本次运行的评分历史（供运行结束汇报）
        self.low_streak = 0        # 连续低置信步数
        self.interventions = 0     # 人工介入次数

    # ---------------- 评分 ----------------

    def score(self, step, signals=None):
        """给一步打 0~5 的置信度，并给出可读归因。

        参数
        ----
        step : dict
            任务流程里的步骤（task_flow.json 的一条）。
        signals : dict
            由调用方预探测得到的信号，字段（全部可选）：
              tier        "T1"~"T4"，来自 tier_resolver.describe_tier
              probe       {"found":bool,"count":int,"strategy":str,
                           "source":"repo"/"inline","used_stale":bool}
              element     元素库条目 dict（含 hit_count/miss_streak/stale）
              history_rate 该步骤历史成功率 0~1（None 表示无历史）
              has_verify  该步骤是否配了结果校验
              env         env_guard 的检测结果 {"kind":...}
        """
        signals = signals or {}
        expl = []
        cap = None

        tier = signals.get("tier") or parse_tier(signals.get("tier_desc"))
        base = self.tier_base.get(tier or "", self.tier_base["_default"])
        score = base
        if tier:
            expl.append("[基准 %.1f] %s 路径（%s）" % (base, tier, TIER_LABEL.get(tier, "")))
        else:
            expl.append("[基准 %.1f] 路径层级未知" % base)

        # ---- 1) 定位器质量：唯一命中是最强的正信号 ----
        probe = signals.get("probe") or {}
        found, count = probe.get("found"), probe.get("count")
        if found is True:
            if count == 1:
                score += 0.5
                expl.append("[+0.50] 定位唯一：只匹配到 1 个元素")
            elif count and count > 1:
                score -= 1.2
                expl.append("[-1.20] 有 %d 个元素都匹配，存在点错的风险" % count)
            else:
                score -= 0.4
                expl.append("[-0.40] 定位命中了但匹配数量不明")
        elif found is False:
            score -= 1.8
            expl.append("[-1.80] 元素库里没有任何定位器能匹配到元素")

        if probe.get("source") == "inline":
            score -= 0.3
            expl.append("[-0.30] 回退到内联选择器（元素库未生效）")
        if probe.get("used_stale"):
            score -= 0.6
            expl.append("[-0.60] 正在使用已被自动遗忘的定位策略")

        # ---- 2) 定位策略稳定度 ----
        strat = probe.get("strategy")
        if strat in self.strategy_adj:
            a = self.strategy_adj[strat]
            if abs(a) >= 0.1:
                score += a
                expl.append("[%+.2f] 定位策略 %s 的固有稳定度" % (a, strat))

        # ---- 3) 元素健康度（历史证据） ----
        el = signals.get("element")
        if el:
            hc = el.get("hit_count", 0) or 0
            ms = el.get("miss_streak", 0) or 0
            if el.get("stale"):
                score -= 1.5
                expl.append("[-1.50] 该元素已被自动遗忘（连续 %d 次未命中）" % ms)
            elif ms >= 3:
                score -= 0.9
                expl.append("[-0.90] 最近连续 %d 次都没找到这个元素" % ms)
            elif ms > 0:
                score -= 0.4
                expl.append("[-0.40] 最近有 %d 次未命中记录" % ms)
            elif hc > 0:
                score += 0.4
                expl.append("[+0.40] 历史上稳定命中过 %d 次" % hc)

        # ---- 4) 步骤历史成功率（同指纹步骤的真实战绩） ----
        rate = signals.get("history_rate")
        if rate is not None:
            adj = round((float(rate) - 0.9) * 1.5, 2)
            if abs(adj) >= 0.15:
                score += adj
                expl.append("[%+.2f] 该步骤历史成功率 %.0f%%" % (adj, float(rate) * 100))
            else:
                expl.append("[  0.00] 该步骤历史成功率 %.0f%%（接近正常水平）" % (float(rate) * 100))

        # ---- 5) 可验证性：能发现失败，才不会「假成功」 ----
        if signals.get("has_verify"):
            score += 0.3
            expl.append("[+0.30] 配了结果校验（失败会被发现，不会假成功）")

        # ---- 6) 环境守卫：异常时直接封顶 ----
        env = signals.get("env") or {}
        kind = env.get("kind")
        if kind and kind != "ok":
            cap = ENV_CAP.get(kind, ENV_CAP["unknown"])
            expl.append("[封顶 %.1f] %s：%s"
                         % (cap, ENV_LABEL.get(kind, kind), env.get("detail") or ""))

        raw = score
        score = max(CONF_MIN, min(CONF_MAX, score))
        if cap is not None and score > cap:
            score = cap
        score = round(score, 2)
        level, action = self._classify(score)

        res = {
            "score": score,
            "raw_score": round(raw, 2),
            "level": level,
            "level_label": LEVEL_LABEL.get(level, level),
            "action": action,
            "explain": expl,
            "capped_by": None if cap is None else {"kind": kind, "cap": cap},
            "signals": {
                "tier": tier,
                "locator_found": found,
                "locator_count": count,
                "strategy": strat,
                "history_rate": rate,
                "has_verify": bool(signals.get("has_verify")),
                "env": kind or "ok",
            },
        }
        self.history.append(res)
        return res

    def _classify(self, score):
        if score >= self.auto_above:
            level, action = LEVEL_HIGH, ACTION_AUTO
        elif score >= self.confirm_below:
            level, action = LEVEL_MEDIUM, ACTION_AUTO
        elif score >= self.abort_below:
            level, action = LEVEL_LOW, ACTION_CONFIRM
        else:
            level, action = LEVEL_CRITICAL, ACTION_ABORT

        # 模式裁剪：off / shadow 都从不阻断（shadow 是零风险观察期）
        if not self.enabled or self.mode == "off":
            action = ACTION_AUTO
        elif self.mode == "shadow" and action in (ACTION_CONFIRM, ACTION_ABORT):
            action = ACTION_AUTO
        return level, action

    # ---------------- 展示 ----------------

    @staticmethod
    def short(res):
        """一行摘要，如：置信 3.40/5 中 ✔"""
        if not res:
            return ""
        return "置信 %.2f/5 %s %s" % (res["score"], res["level_label"],
                                     LEVEL_ICON.get(res["level"], ""))

    @staticmethod
    def detail_lines(res, width=72):
        """把归因展开成多行，供现场打印。"""
        if not res:
            return []
        lines = []
        for e in res.get("explain", []):
            lines.append("       " + e)
        return lines

    def note_low(self, is_low):
        """累计连续低置信步数；返回 True 表示已达到需要停下来诊断的程度。"""
        if is_low:
            self.low_streak += 1
        else:
            self.low_streak = 0
        return self.low_streak >= self.max_low_conf_streak

    def summary(self):
        """本次运行的置信度概览（结束时打印）。"""
        n = len(self.history)
        if not n:
            return None
        buckets = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 0, LEVEL_LOW: 0, LEVEL_CRITICAL: 0}
        for r in self.history:
            buckets[r["level"]] = buckets.get(r["level"], 0) + 1
        scores = [r["score"] for r in self.history]
        low = [r for r in self.history if r["level"] in (LEVEL_LOW, LEVEL_CRITICAL)]
        return {
            "count": n,
            "avg": round(sum(scores) / n, 2),
            "min": min(scores),
            "buckets": buckets,
            "low_count": len(low),
            "interventions": self.interventions,
            "low_steps": low[:5],
        }


def build_from_config(cfg, repo=None, stats=None, env_guard=None):
    """从 config.json 的 confidence 段构造评分器。"""
    return ConfidenceScorer(cfg or {}, repo=repo, stats=stats, env_guard=env_guard)
