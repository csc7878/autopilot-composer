# -*- coding: utf-8 -*-
"""步骤级结果校验（Verify）—— v3.4.1

借鉴 GUI-Agent-Harness 的 Observe → **Verify** → Plan → Dispatch 循环：
每步操作后校验「是否真的生效」，而不是默认成功。

为什么需要它：确定性回放在"点击成功"与"操作生效"之间有断层——
CSS 找得到按钮、CDP 点击也没报错，但页面可能弹了错误提示、按钮是灰的、
或请求被后端拒绝。v3.4.1 之前只有 T1 直连层有断言（assertions），
界面路径完全没有结果校验；失败要等到后面某一步莫名报错才暴露。

校验规格写在 task_flow.json 的步骤里（单个 dict 或 dict 数组，全部通过才算过）：

    {
      "type": "browser", "func": "click_elem", "args": ["#btn-save"],
      "verify": [
        {"type": "text_present",   "text": "保存成功"},
        {"type": "element_absent", "selector": ".loading-mask"}
      ]
    }

支持的校验类型：
    element_exists        selector  元素出现（count>0）
    element_absent        selector  元素消失
    element_count         selector / count  元素个数等于 count
    text_present          text      页面可见文本包含
    text_absent           text      页面可见文本不包含
    url_contains          value     当前 URL 包含子串
    url_equals            value     当前 URL 等于（忽略末尾斜杠）
    element_value_equals  selector / value   元素 value/innerText 等于
    element_value_contains selector / value  元素 value/innerText 包含
    window_title          value     前台窗口标题包含（桌面场景）
    file_exists           path      文件已生成（导出/下载场景）

轮询语义：默认在 timeout 秒内每 interval 秒重试一次（应对页面异步渲染），
超时仍未通过即判定校验失败 → 该步按失败处理（重试 / 降级 / 断点暂停）。
"""
import os
import time

DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 0.3

# 各类型的说明文案（用于日志/报错）
_TYPE_LABEL = {
    "element_exists": "元素应出现",
    "element_absent": "元素应消失",
    "element_count": "元素个数应相等",
    "text_present": "页面应包含文本",
    "text_absent": "页面应不含文本",
    "url_contains": "URL 应包含",
    "url_equals": "URL 应等于",
    "element_value_equals": "元素值应等于",
    "element_value_contains": "元素值应包含",
    "window_title": "前台窗口标题应包含",
    "file_exists": "文件应已生成",
}

SUPPORTED_TYPES = tuple(_TYPE_LABEL.keys())


def _pick(spec, *keys, **kw):
    """从 spec 里取第一个存在的键（兼容 selector/query、text/contains 等别名）。"""
    for k in keys:
        if k in spec and spec[k] is not None:
            return spec[k]
    return kw.get("default")


def normalize_spec(spec):
    """把 verify 字段统一成 list（单 dict / dict 数组均可）。"""
    if not spec:
        return []
    if isinstance(spec, dict):
        return [spec]
    if isinstance(spec, (list, tuple)):
        return [s for s in spec if isinstance(s, dict)]
    return []


def _foreground_window_title():
    """取前台窗口标题（纯 ctypes，不依赖 pywin32）。非 Windows 返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


class Verifier:
    """步骤结果校验器。

    browser: CdpBrowserCtrl 实例（网页类校验用），可为 None
    timeout/interval: 轮询窗口与间隔
    """

    def __init__(self, browser=None, timeout=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL):
        self.browser = browser
        self.timeout = timeout
        self.interval = interval

    # ---------------- 对外入口 ----------------

    def check(self, spec, browser=None):
        """执行校验，返回 {"ok":bool, "detail":str, "results":[...], "skipped":bool}。"""
        specs = normalize_spec(spec)
        if not specs:
            return {"ok": True, "detail": "无校验项", "results": [], "skipped": True}

        browser = browser or self.browser
        results = []
        for s in specs:
            ok, detail, skipped = self._check_one(s, browser)
            results.append({"spec": s, "ok": ok, "detail": detail, "skipped": skipped})

        failed = [r for r in results if not r["ok"]]
        skipped = all(r["skipped"] for r in results) if results else False
        if failed:
            detail = "；".join(r["detail"] for r in failed)
            return {"ok": False, "detail": detail, "results": results, "skipped": False}
        return {"ok": True,
                "detail": "；".join(r["detail"] for r in results),
                "results": results, "skipped": skipped}

    def check_step(self, step, browser=None):
        """直接从 task_flow 的步骤 dict 读 verify 字段并校验。"""
        return self.check((step or {}).get("verify"), browser=browser)

    # ---------------- 内部实现 ----------------

    def _check_one(self, spec, browser):
        """单条校验（带轮询）。返回 (ok, detail, skipped)。"""
        stype = spec.get("type", "")
        if stype not in SUPPORTED_TYPES:
            return False, "未知校验类型: %s（支持：%s）" % (stype, "/".join(SUPPORTED_TYPES)), False

        timeout = float(spec.get("timeout", self.timeout))
        deadline = time.time() + max(0.0, timeout)
        last_detail = ""
        while True:
            ok, detail, skipped = self._eval_once(stype, spec, browser)
            if ok or skipped:
                return ok, detail, skipped
            last_detail = detail
            if time.time() >= deadline:
                return False, last_detail, False
            time.sleep(self.interval)

    def _eval_once(self, stype, spec, browser):
        """执行一次判定（不轮询）。返回 (ok, detail, skipped)。"""
        label = _TYPE_LABEL.get(stype, stype)

        # ---- 网页类校验 ----
        if stype in ("element_exists", "element_absent", "element_count",
                     "text_present", "text_absent", "url_contains", "url_equals",
                     "element_value_equals", "element_value_contains"):
            if browser is None:
                return True, "跳过（浏览器未连接）", True

        if stype == "element_exists":
            sel = _pick(spec, "selector", "query")
            n = browser.element_count(sel)
            return (n > 0), "%s %r（实际 %d 个）" % (label, sel, n), False

        if stype == "element_absent":
            sel = _pick(spec, "selector", "query")
            n = browser.element_count(sel)
            return (n == 0), "%s %r（实际 %d 个）" % (label, sel, n), False

        if stype == "element_count":
            sel = _pick(spec, "selector", "query")
            want = int(_pick(spec, "count", "expected", default=1))
            n = browser.element_count(sel)
            return (n == want), "%s %r（期望 %d，实际 %d）" % (label, sel, want, n), False

        if stype in ("text_present", "text_absent"):
            want = str(_pick(spec, "text", "contains", "value", default=""))
            txt = browser.page_text()
            hit = want in txt
            ok = hit if stype == "text_present" else (not hit)
            return ok, "%s「%s」（%s）" % (label, want, "命中" if hit else "未命中"), False

        if stype == "url_contains":
            want = str(_pick(spec, "value", "url", "text", default=""))
            url = browser.current_url()
            return (want in url), "%s「%s」（当前 %s）" % (label, want, url[:120]), False

        if stype == "url_equals":
            want = str(_pick(spec, "value", "url", default="")).rstrip("/")
            url = (browser.current_url() or "").rstrip("/")
            return (url == want), "%s「%s」（当前 %s）" % (label, want, url[:120]), False

        if stype in ("element_value_equals", "element_value_contains"):
            sel = _pick(spec, "selector", "query")
            want = str(_pick(spec, "value", "text", "expected", default=""))
            val = browser.element_value(sel)
            if val is None:
                return False, "%s %r：元素未找到" % (label, sel), False
            if stype == "element_value_equals":
                ok = (str(val) == want)
            else:
                ok = (want in str(val))
            return ok, "%s %r（期望「%s」，实际「%s」）" % (label, sel, want, str(val)[:60]), False

        # ---- 桌面 / 文件类校验 ----
        if stype == "window_title":
            want = str(_pick(spec, "value", "title", "text", default=""))
            title = _foreground_window_title()
            return (want in title), "%s「%s」（当前「%s」）" % (label, want, title[:80]), False

        if stype == "file_exists":
            path = _pick(spec, "path", "value", default="")
            ok = bool(path) and os.path.exists(path)
            return ok, "%s %s" % (label, path), False

        return False, "未实现的校验类型: %s" % stype, False


if __name__ == "__main__":
    # 自检：不依赖浏览器，验证调度与文件类校验
    import json
    import tempfile
    v = Verifier(timeout=0.5, interval=0.1)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    tmp.write(b"x")
    tmp.close()
    cases = [
        ({"type": "file_exists", "path": tmp.name}, True),
        ({"type": "file_exists", "path": tmp.name + ".nope"}, False),
        ({"type": "unknown_type"}, False),
        ([{"type": "file_exists", "path": tmp.name},
          {"type": "file_exists", "path": tmp.name + ".nope"}], False),
    ]
    all_ok = True
    for spec, expect in cases:
        res = v.check(spec)
        flag = "OK " if res["ok"] == expect else "FAIL"
        if res["ok"] != expect:
            all_ok = False
        print("%s expect=%s got=%s | %s" % (flag, expect, res["ok"], res["detail"]))
    os.unlink(tmp.name)
    print(json.dumps({"self_test": "pass" if all_ok else "FAIL"}, ensure_ascii=False))
