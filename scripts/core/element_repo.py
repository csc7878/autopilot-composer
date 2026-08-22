# -*- coding: utf-8 -*-
"""元素库（Element Repository）。

把录制过程中遇到的元素集中登记到 elements.json，每个元素存一份「多策略定位器电池」。
回放时主路径走稳定定位器，失败自动回退；改一处全局生效（直接消灭脆弱的链式
选择器 + 临时打 id 的问题）。

稳定性评分：定位器越多元越稳（id>name>placeholder... 组合越多，抗改版能力越强）。
"""
import json
import os
import uuid


class ElementRepository:
    def __init__(self, path=None):
        self.path = path
        self.elements = {}   # eid -> {id,name,domain,kind,locators,stability,note,used}

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
            "locators": locators,
            "stability": round(stability, 1),
            "note": note,
            "used": 0,
        }
        return eid

    def get(self, eid):
        return self.elements.get(eid)

    def inc_used(self, eid):
        el = self.elements.get(eid)
        if el:
            el["used"] += 1

    def best_locator(self, eid, priority):
        """返回该元素在 priority 顺序下最优的定位器 {strategy,query} 或 None。"""
        el = self.get(eid)
        if not el:
            return None
        by_strategy = {l["strategy"]: l["query"] for l in el["locators"]}
        for strat in priority:
            if strat in by_strategy and by_strategy[strat]:
                return {"strategy": strat, "query": by_strategy[strat]}
        return None

    def to_payload(self):
        return {"version": 1, "elements": list(self.elements.values())}

    def save(self, path=None):
        path = path or self.path
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_payload(), f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path):
        """加载「用户元素库」格式：{version, elements:[{id,...}]}。"""
        repo = cls(path)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                for e in data.get("elements", []):
                    repo.elements[e["id"]] = e
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
                "locators": locators,
                "stability": round(max(3, min(10, 3 + len(locators) * 1.5)), 1),
                "note": "preset:" + key,
                "used": 0,
            }
        return repo

    def merge(self, other):
        """把 other 的元素并入自身（已存在 eid 不覆盖）。"""
        for eid, el in other.elements.items():
            if eid not in self.elements:
                self.elements[eid] = el
        return self
