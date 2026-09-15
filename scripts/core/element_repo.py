# -*- coding: utf-8 -*-
"""元素库（Element Repository）。

把录制过程中遇到的元素集中登记到 elements.json，每个元素存一份「多策略定位器电池」。
回放时主路径走稳定定位器，失败自动回退；改一处全局生效（直接消灭脆弱的链式
选择器 + 临时打 id 的问题）。

稳定性评分：定位器越多元越稳（id>name>placeholder... 组合越多，抗改版能力越强）。

v3.4.1 新增「健康度跟踪 + 自动遗忘」（借鉴 GUI-Agent-Harness 的视觉记忆机制）：
  - 元素级：hit_count（命中次数）/ miss_streak（连续未命中）/ last_ok_ts / stale
  - 定位器级：每个策略单独计数，连续未命中达 locator_stale_threshold 次即自动
    「遗忘」（stale=True，回放时跳过该策略），避免脆弱的链式选择器一直拖后腿
  - 连续未命中达 stale_threshold 次的元素标记 stale（不删除，保留人工复核）
  - 任一命中即复活（miss_streak 归零、stale 清除），适配界面改版后又改回来
  - health_report() 输出健康度概览，main_task.py --health 可直接查看

设计取舍：只「降权/跳过」不「删除」——自动删除会丢失唯一的定位线索，
标记 stale 既能让回放避开它，又保留人工复核与恢复的可能。
"""
import json
import os
import time
import uuid

# 连续未命中多少次后判定「该遗忘」
DEFAULT_STALE_THRESHOLD = 15          # 元素级（与 GUI-Agent-Harness 的 15 次一致）
DEFAULT_LOCATOR_STALE_THRESHOLD = 5   # 定位器级（更敏感，单个策略错几次就该让位）


def _now_ms():
    return int(time.time() * 1000)


def new_locator(strategy, query, **extra):
    """构造一个带健康计数的定位器条目。"""
    d = {"strategy": strategy, "query": query,
         "hit": 0, "miss": 0, "miss_streak": 0, "stale": False}
    d.update(extra)
    return d


def ensure_locator_health(loc):
    """兼容旧数据：补齐定位器的健康计数字段（原地修改并返回）。"""
    loc.setdefault("hit", 0)
    loc.setdefault("miss", 0)
    loc.setdefault("miss_streak", 0)
    loc.setdefault("stale", False)
    return loc


def record_locator_hit(element, strategy):
    """记录某定位器策略命中（原地修改 element dict）。"""
    if not element or not strategy:
        return
    for loc in element.get("locators", []):
        if loc.get("strategy") == strategy:
            ensure_locator_health(loc)
            loc["hit"] += 1
            loc["miss_streak"] = 0
            loc["stale"] = False
            return


def record_locator_miss(element, strategy, threshold=DEFAULT_LOCATOR_STALE_THRESHOLD):
    """记录某定位器策略未命中；连续未命中达阈值即标记 stale（自动遗忘该策略）。

    返回 True 表示本次调用触发了「新遗忘」（调用方可据此记日志）。
    """
    if not element or not strategy:
        return False
    for loc in element.get("locators", []):
        if loc.get("strategy") == strategy:
            ensure_locator_health(loc)
            loc["miss"] += 1
            loc["miss_streak"] += 1
            if loc["miss_streak"] >= threshold and not loc["stale"]:
                loc["stale"] = True
                return True
            return False
    return False


class ElementRepository:
    def __init__(self, path=None,
                 stale_threshold=DEFAULT_STALE_THRESHOLD,
                 locator_stale_threshold=DEFAULT_LOCATOR_STALE_THRESHOLD):
        self.path = path
        self.elements = {}   # eid -> {id,name,domain,kind,locators,stability,note,used,+health}
        self.stale_threshold = stale_threshold
        self.locator_stale_threshold = locator_stale_threshold

    # ---------------- 登记 ----------------

    def register(self, domain, name, locators, kind="web", note=""):
        if not locators:
            return None
        eid = "el_" + uuid.uuid4().hex[:8]
        # 定位器越多越稳：基础 3 分，每个定位器 +1.5，上限 10
        stability = max(3, min(10, 3 + len(locators) * 1.5))
        self.elements[eid] = {
            "id": eid,
            "domain": domain,
            "name": (name or "element")[:40],
            "kind": kind,
            "locators": [ensure_locator_health(dict(l)) for l in locators],
            "stability": round(stability, 1),
            "note": note,
            "used": 0,
            "hit_count": 0,
            "miss_streak": 0,
            "last_ok_ts": None,
            "stale": False,
            "stale_reason": "",
        }
        return eid

    def get(self, eid):
        return self.elements.get(eid)

    def inc_used(self, eid):
        el = self.elements.get(eid)
        if el:
            # 用 get 兜底：兼容缺 used 字段的旧库/手工库（否则重试路径会 KeyError）
            el["used"] = el.get("used", 0) + 1

    # ---------------- 健康度跟踪（v3.4.1） ----------------

    @staticmethod
    def ensure_health(el):
        """兼容旧元素库：补齐健康字段（原地修改并返回）。"""
        if not el:
            return el
        el.setdefault("used", 0)   # 手工编辑/第三方生成的库可能缺这个字段
        el.setdefault("hit_count", 0)
        el.setdefault("miss_streak", 0)
        el.setdefault("last_ok_ts", None)
        el.setdefault("stale", False)
        el.setdefault("stale_reason", "")
        for loc in el.get("locators", []):
            ensure_locator_health(loc)
        return el

    def is_stale(self, eid):
        el = self.get(eid)
        return bool(el and el.get("stale"))

    def record_hit(self, eid, strategy=None):
        """元素命中：累加 hit_count、清零 miss_streak、复活 stale。"""
        el = self.elements.get(eid)
        if not el:
            return False
        self.ensure_health(el)
        was_stale = el.get("stale")
        el["hit_count"] += 1
        el["miss_streak"] = 0
        el["last_ok_ts"] = _now_ms()
        el["stale"] = False
        el["stale_reason"] = ""
        record_locator_hit(el, strategy)
        return bool(was_stale)   # 返回 True 表示「复活了一个已遗忘的元素」

    def record_miss(self, eid, strategy=None):
        """元素未命中：累加 miss_streak，达阈值标记 stale。

        返回 {"stale_now": bool, "locator_stale_now": bool} 便于调用方记日志。
        """
        el = self.elements.get(eid)
        if not el:
            return {"stale_now": False, "locator_stale_now": False}
        self.ensure_health(el)
        el["miss_streak"] += 1
        stale_now = False
        if el["miss_streak"] >= self.stale_threshold and not el["stale"]:
            el["stale"] = True
            el["stale_reason"] = "连续 %d 次未命中（自动遗忘，可 --health 复核）" % el["miss_streak"]
            stale_now = True
        locator_stale_now = record_locator_miss(el, strategy, self.locator_stale_threshold)
        return {"stale_now": stale_now, "locator_stale_now": locator_stale_now}

    def reset_health(self, eid=None, only_stale=True):
        """人工复核后恢复：清空健康计数与 stale 标记。

        only_stale=True 时只重置被自动遗忘的元素（默认，最安全）。
        """
        targets = [eid] if eid else list(self.elements.keys())
        n = 0
        for t in targets:
            el = self.elements.get(t)
            if not el:
                continue
            self.ensure_health(el)
            if only_stale and not el.get("stale"):
                continue
            el["miss_streak"] = 0
            el["stale"] = False
            el["stale_reason"] = ""
            for loc in el.get("locators", []):
                loc["miss_streak"] = 0
                loc["stale"] = False
            n += 1
        return n

    def health_report(self):
        """元素库健康度概览（供 python main_task.py --health 展示）。

        weakest 只列「本次或历史回放中真的失败过」的元素（miss_streak>0）；
        从未回放过的元素单独计入 never_used，避免把新录的元素误报成问题元素。
        """
        total = len(self.elements)
        stale_ids = []
        weak = []
        never_used = 0
        dead_locator_els = 0
        for eid, el in self.elements.items():
            self.ensure_health(el)
            if el.get("stale"):
                stale_ids.append(eid)
            dead_loc = [l.get("strategy") for l in el.get("locators", []) if l.get("stale")]
            if dead_loc:
                dead_locator_els += 1
            if el.get("hit_count", 0) == 0 and el.get("miss_streak", 0) == 0:
                never_used += 1
                continue
            if el.get("miss_streak", 0) > 0 or dead_loc:
                weak.append({
                    "id": eid,
                    "name": " ".join(str(el.get("name", "")).split()),
                    "domain": el.get("domain", ""),
                    "hit_count": el.get("hit_count", 0),
                    "miss_streak": el.get("miss_streak", 0),
                    "stale_locators": dead_loc,
                    "locators": len(el.get("locators", [])),
                })
        weak.sort(key=lambda x: (-x["miss_streak"], x["hit_count"]))
        return {
            "total": total,
            "stale": len(stale_ids),
            "healthy": total - len(stale_ids),
            "never_used": never_used,
            "dead_locator_elements": dead_locator_els,
            "stale_ids": stale_ids,
            "weakest": weak[:10],
        }

    # ---------------- 定位器选择 ----------------

    def best_locator(self, eid, priority):
        """返回该元素在 priority 顺序下最优的定位器 {strategy,query} 或 None。

        已被自动遗忘（stale）的定位器策略会被跳过；若全部策略都已 stale，
        则回退到原始优先顺序（保证「有线索总比没有好」，不至于直接失败）。
        """
        el = self.get(eid)
        if not el:
            return None
        self.ensure_health(el)
        by_strategy = {}
        stale_strategies = set()
        for l in el["locators"]:
            by_strategy[l["strategy"]] = l["query"]
            if l.get("stale"):
                stale_strategies.add(l["strategy"])

        for strat in priority:
            if strat in by_strategy and by_strategy[strat] and strat not in stale_strategies:
                return {"strategy": strat, "query": by_strategy[strat]}

        # 全部已遗忘：兜底回退到原顺序（并提醒调用方）
        for strat in priority:
            if strat in by_strategy and by_strategy[strat]:
                return {"strategy": strat, "query": by_strategy[strat],
                        "all_stale": True}
        return None

    # ---------------- 持久化 ----------------

    def to_payload(self, skip_presets=False):
        els = list(self.elements.values())
        if skip_presets:
            els = [e for e in els if not str(e.get("id", "")).startswith("preset_")]
        return {
            "version": 2,
            "meta": {
                "stale_threshold": self.stale_threshold,
                "locator_stale_threshold": self.locator_stale_threshold,
                "saved_at": _now_ms(),
            },
            "elements": els,
        }

    def save(self, path=None, skip_presets=False):
        """写盘。skip_presets=True 时只写用户元素（不把预置库固化进用户库）。"""
        path = path or self.path
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_payload(skip_presets=skip_presets),
                          f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path):
        """加载「用户元素库」格式：{version, elements:[{id,...}]}。"""
        repo = cls(path)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                meta = data.get("meta") or {}
                if meta.get("stale_threshold"):
                    repo.stale_threshold = meta["stale_threshold"]
                if meta.get("locator_stale_threshold"):
                    repo.locator_stale_threshold = meta["locator_stale_threshold"]
                for e in data.get("elements", []):
                    repo.elements[e["id"]] = repo.ensure_health(e)
            except Exception:
                pass
        return repo

    @classmethod
    def load_preset(cls, path):
        """加载「预置元素库」格式：{_meta, web:{key:{name,domain,...,locators}}}。

        预置库没有 eid，这里自动生成 eid，并保留原有 key 作为 name 候选。
        返回 ElementRepository（不写盘）。
        """
        repo = cls(path)
        if not path or not os.path.exists(path):
            return repo
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return repo
        for key, el in (data.get("web") or {}).items():
            locators = el.get("locators", [])
            if not locators:
                continue
            eid = "preset_" + key
            repo.elements[eid] = {
                "id": eid,
                "domain": el.get("domain", "*"),
                "name": el.get("name", key),
                "kind": "web",
                "locators": [ensure_locator_health(dict(l)) for l in locators],
                "stability": round(max(3, min(10, 3 + len(locators) * 1.5)), 1),
                "note": "preset:" + key,
                "used": 0,
                "hit_count": 0,
                "miss_streak": 0,
                "last_ok_ts": None,
                "stale": False,
                "stale_reason": "",
            }
        return repo

    def merge(self, other):
        """把 other 的元素并入自身（已存在 eid 不覆盖）。"""
        for eid, el in other.elements.items():
            if eid not in self.elements:
                self.elements[eid] = el
        return self
