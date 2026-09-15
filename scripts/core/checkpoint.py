# -*- coding: utf-8 -*-
"""执行检查点与回滚（Checkpoint & Rollback）。

理论来源
--------
Enhancing Web Agents with Explicit Rollback Mechanisms（见 dive-into-llms 第9章
slides p40）指出：**「智能体一旦已经陷入错误路径，后续操作就极可能不可靠」**，
因此需要适当的回退机制，避免在错误路径中继续叠加不可靠动作。

与既有 breakpoint.json 的区别
----------------------------
  - `breakpoint.json`（v3.1 起）解决的是**跨进程恢复**：「上次跑到第几步」，
    重启后从哪里接着跑。
  - 本模块解决的是**错误状态回退**：页面已经被带到一个错误状态（误点进了
    无关页面、跳到了错误页），此时即使「第几步」是对的，继续跑也只会一路
    失败。需要先把环境退回上一个已知良好的状态，再重试当前步骤。

刻意保持轻量：只记录 URL / 标题 / 时间戳，**不做截图**。理由是这台机器
（2 核 G4560 / 8GB / 核显）磁盘与算力都紧张，而页面状态能不能用，一次
URL 对比就能判断八成。
"""
import json
import os
import time


def _now_ms():
    return int(time.time() * 1000)


class CheckpointStore:
    """环形检查点列表（默认只保留最近 keep 条）。"""

    def __init__(self, path=None, keep=20):
        self.path = path
        self.keep = int(keep)
        self.items = []     # [{"idx","url","title","app","desc","ts"}]

    # ---------------- 记录 ----------------

    def record_ok(self, idx, url="", title="", app="", desc=""):
        """记录一次「已知良好状态」。（idx 为已完成的步序，0-based）"""
        item = {
            "idx": int(idx),
            "url": url or "",
            "title": title or "",
            "app": app or "",
            "desc": (desc or "")[:60],
            "ts": _now_ms(),
        }
        # 同一步重复记录则覆盖（重试成功后只留最新）
        self.items = [it for it in self.items if it.get("idx") != item["idx"]]
        self.items.append(item)
        if len(self.items) > self.keep:
            self.items = self.items[-self.keep:]
        return item

    # ---------------- 查询 ----------------

    def last_good(self, before_idx=None, need_url=False):
        """取最近一个已知良好状态。

        before_idx : 只考虑步序 < before_idx 的检查点（默认不限）
        need_url   : 只考虑带 URL 的（浏览器回滚用；桌面步骤不带 URL）
        """
        cands = list(self.items)
        if before_idx is not None:
            cands = [it for it in cands if it.get("idx") < int(before_idx)]
        if need_url:
            cands = [it for it in cands if it.get("url")]
        return cands[-1] if cands else None

    def clear(self):
        self.items = []

    def summary(self):
        return {"count": len(self.items),
                "last": self.items[-1] if self.items else None}

    # ---------------- 持久化 ----------------

    def to_payload(self):
        return {"version": 1, "keep": self.keep,
                "saved_at": _now_ms(), "items": self.items}

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
    def load(cls, path, keep=20):
        st = cls(path, keep=keep)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                st.items = data.get("items") or []
                st.keep = data.get("keep", keep)
            except Exception:
                pass
        return st
