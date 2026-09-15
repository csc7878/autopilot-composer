# -*- coding: utf-8 -*-
"""步骤执行统计（Step Statistics）——给置信度提供「历史战绩」依据。

为什么需要它
------------
OS-Kairos 的自适应介入依赖模型对每步的置信度预测。确定性 RPA 没有模型，但
有一个模型没有的东西：**同一份流程被反复回放的真实战绩**。一个步骤过去跑了
50 次都成功，它今天大概率也会成功；一个步骤过去失败率 60%，今天就该被重点
关照。这是最朴素也最可靠的先验。

设计要点
--------
  - **指纹索引**：只取步骤的稳定语义字段（type/func/args/element_ref）算 MD5，
    忽略 id / ts / status 等易变字段——否则每次录制的 id 变化都会让统计失忆。
  - **样本保护**：样本数 < min_samples（默认 2）时返回 None，让评分器不给
    加成也不给惩罚。避免「跑过一次失败就被永久打上低置信标签」。
  - 与 operation_log.json（逐次执行审计）互补：后者是明细流水，本模块是
    聚合后的成功率视图，且只保留少量计数，体积可控。
"""
import hashlib
import json
import os
import time

DEFAULT_MIN_SAMPLES = 2


def _now_ms():
    return int(time.time() * 1000)


def step_fingerprint(step):
    """计算步骤指纹（只取稳定语义字段）。"""
    if not step:
        return ""
    key = {
        "t": step.get("type"),
        "f": step.get("func"),
        "a": step.get("args", step.get("params")),
        "e": step.get("element_ref"),
    }
    s = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


class StepStats:
    """按步骤指纹聚合的成功率统计。"""

    def __init__(self, path=None, min_samples=DEFAULT_MIN_SAMPLES):
        self.path = path
        self.min_samples = min_samples
        self.data = {}   # fp -> {ok, fail, last_ok_ts, last_fail_ts, desc, last_err}

    # ---------------- 记录 ----------------

    def record(self, step, ok, desc="", err=""):
        """记录一次执行结果。"""
        fp = step_fingerprint(step)
        if not fp:
            return
        d = self.data.setdefault(fp, {"ok": 0, "fail": 0,
                                      "last_ok_ts": None, "last_fail_ts": None,
                                      "desc": "", "last_err": ""})
        now = _now_ms()
        if ok:
            d["ok"] += 1
            d["last_ok_ts"] = now
        else:
            d["fail"] += 1
            d["last_fail_ts"] = now
            if err:
                d["last_err"] = str(err)[:200]
        if desc:
            d["desc"] = str(desc)[:60]

    # ---------------- 查询 ----------------

    def success_rate(self, step):
        """返回该步骤历史成功率（0~1）。样本不足返回 None（不影响评分）。"""
        d = self.data.get(step_fingerprint(step))
        if not d:
            return None
        total = d.get("ok", 0) + d.get("fail", 0)
        if total < self.min_samples:
            return None
        return round(float(d.get("ok", 0)) / total, 4)

    def samples(self, step):
        d = self.data.get(step_fingerprint(step))
        if not d:
            return 0
        return d.get("ok", 0) + d.get("fail", 0)

    def summary(self):
        """整体概览（供 --report / 运行结束汇报）。"""
        total_ok = sum(d.get("ok", 0) for d in self.data.values())
        total_fail = sum(d.get("fail", 0) for d in self.data.values())
        total = total_ok + total_fail
        worst = []
        for fp, d in self.data.items():
            t = d.get("ok", 0) + d.get("fail", 0)
            if t >= self.min_samples and d.get("fail", 0) > 0:
                worst.append({
                    "fp": fp,
                    "desc": d.get("desc", ""),
                    "ok": d.get("ok", 0),
                    "fail": d.get("fail", 0),
                    "rate": round(float(d.get("ok", 0)) / t, 3),
                    "last_err": d.get("last_err", ""),
                })
        worst.sort(key=lambda x: (x["rate"], -x["fail"]))
        return {
            "steps": len(self.data),
            "runs_ok": total_ok,
            "runs_fail": total_fail,
            "success_rate": round(float(total_ok) / total, 4) if total else None,
            "worst": worst[:10],
        }

    # ---------------- 持久化 ----------------

    def to_payload(self):
        return {"version": 1, "min_samples": self.min_samples,
                "saved_at": _now_ms(), "steps": self.data}

    def save(self, path=None):
        path = path or self.path
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_payload(), f, ensure_ascii=False, indent=2)
        except Exception:
            return None
        return path

    @classmethod
    def load(cls, path, min_samples=DEFAULT_MIN_SAMPLES):
        st = cls(path, min_samples=min_samples)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                st.data = data.get("steps") or {}
                st.min_samples = data.get("min_samples", min_samples)
            except Exception:
                pass
        return st

    def clear(self):
        self.data = {}
