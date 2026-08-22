import websocket
import json
import base64
import time
import os
import urllib.request

from core.locator import is_xpath, WEB_PRIORITY


class CdpBrowserCtrl:
    """浏览器 CDP 控制引擎（page 级直连通道）。

    通过 Chrome 远程调试端口（默认 9222）的 HTTP 接口发现已打开的
    标签页，直连其 webSocketDebuggerUrl，从而真正驱动页面导航、
    元素输入、点击与截图。无需浏览器级 /devtools/browser 端点
    （Chrome 111+ 默认拒绝其 WebSocket 握手，故走 page 级通道）。

    前置条件：
      - 已启动 Chrome 并开启远程调试：
        chrome --remote-debugging-port=9222 --remote-allow-origins=*
      - 至少存在一个已打开的标签页（about:blank 也可）
    """

    def __init__(self, port=9222):
        self.port = port
        self.http = f"http://127.0.0.1:{port}"
        self.ws = None
        self._id = 0

    # ---------- 发现与连接 ----------
    def _list_targets(self):
        with urllib.request.urlopen(self.http + "/json", timeout=5) as r:
            return json.load(r)

    def connect(self, target_type="page", url_filter=None):
        """发现并直连一个可用标签页的 WebSocket。

        url_filter：可选，传入子串/正则匹配的 URL，连到命中该模式的页面
        （多标签页场景下可用于精准操作录制时所在的页面）。
        """
        if self.ws is not None:
            return self.ws
        targets = self._list_targets()
        cand = [t for t in targets
                if t.get("type") == target_type and t.get("webSocketDebuggerUrl")]
        if not cand:
            cand = [t for t in targets if t.get("webSocketDebuggerUrl")]
        if url_filter:
            import re
            matched = [t for t in cand
                       if re.search(url_filter, t.get("url", "") or "")]
            if matched:
                cand = matched
        if not cand:
            raise RuntimeError(
                "未找到可连接的浏览器标签页，请先在 Chrome 中打开一个页面"
            )
        ws_url = cand[0]["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=10)
        return self.ws

    # ---------- 底层通信 ----------
    def _next_id(self):
        self._id += 1
        return self._id

    def send_cmd(self, method, params=None):
        """发送一条 CDP 命令并等待匹配 id 的响应（跳过无 id 的事件帧）。"""
        if params is None:
            params = {}
        self.connect()
        msg_id = self._next_id()
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            data = json.loads(self.ws.recv())
            if "id" not in data:
                continue
            if data["id"] == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error on {method}: {data['error']}")
                return data.get("result", {})

    # ---------- 页面交互 ----------
    def open_url(self, url):
        """在当前标签页导航到 url，并等待导航提交 + 页面加载完成。"""
        self.send_cmd("Page.enable")
        self.send_cmd("Page.navigate", {"url": url})
        self._wait_navigated(url)

    def navigate(self, url):
        """等同于 open_url。"""
        self.open_url(url)

    @staticmethod
    def _norm_url(u):
        return (u or "").rstrip("/")

    def _wait_navigated(self, url, timeout=15):
        """等待导航真正提交且新文档加载完成。

        不能用简单的 document.readyState 轮询：Page.navigate 之后的一段时间内，
        Runtime.evaluate 仍可能跑在「旧文档」的上下文里，而旧文档的 readyState
        已经是 complete，导致误判。这里改为同时校验 location.href 已切换到目标
        URL 且 readyState 为 complete，确保后续操作命中新页面。
        """
        target = self._norm_url(url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                res = self.send_cmd(
                    "Runtime.evaluate",
                    {"expression": "({h: location.href, rs: document.readyState})",
                     "returnByValue": True},
                )
                d = res.get("result", {}).get("value") or {}
                if self._norm_url(d.get("h")) == target and d.get("rs") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.2)

    # ---------- 元素定位辅助（兼容 CSS / XPath） ----------
    def _el_expr(self, selector):
        """返回一段 JS 表达式，求值后得到元素或 null，兼容 CSS 与 XPath。"""
        if is_xpath(selector):
            return ('(function(){var r=document.evaluate(%r,document,null,'
                    'XPathResult.FIRST_ORDERED_NODE_TYPE,null);'
                    'return r?r.singleNodeValue:null;})()' % selector)
        return 'document.querySelector(%r)' % selector

    def _probe(self, selector):
        """探测元素：返回 {count, x, y, found}。同时支持 CSS 与 XPath。"""
        use_xpath = is_xpath(selector)
        if use_xpath:
            cnt = ('document.evaluate(%r,document,null,'
                   'XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null).snapshotLength' % selector)
            node = ('document.evaluate(%r,document,null,'
                    'XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue' % selector)
        else:
            cnt = "document.querySelectorAll(%r).length" % selector
            node = "document.querySelectorAll(%r)[0]" % selector
        js = (
            "(() => {"
            "  var count = %s;"
            "  var el = %s;"
            "  if (!el) return {count:0, x:0, y:0, found:false};"
            "  var r = el.getBoundingClientRect();"
            "  return {count: count, x: r.left + r.width/2, y: r.top + r.height/2, found:true};"
            "})()"
        ) % (cnt, node)
        res = self.send_cmd(
            "Runtime.evaluate", {"expression": js, "returnByValue": True}
        )
        return res.get("result", {}).get("value") or {"count": 0, "x": 0, "y": 0, "found": False}

    def resolve_locator(self, element, priority=None):
        """给定元素库里的元素 dict，按 priority 依次尝试各定位器。

        返回首个「能唯一匹配（count==1）」的 query；若都多匹配/未匹配，
        则 best-effort 返回第一个命中的 query；全失败返回 None。
        回放器据此在稳定性与可用性间自动权衡（对应影刀「智能元素」思路）。
        """
        if priority is None:
            priority = WEB_PRIORITY
        if not element:
            return None
        locs = {l["strategy"]: l["query"] for l in element.get("locators", [])}
        best = None
        for strat in priority:
            q = locs.get(strat)
            if not q:
                continue
            try:
                p = self._probe(q)
            except Exception:
                continue
            if p.get("found"):
                if p.get("count") == 1:
                    return q
                best = q
        return best

    def input_text(self, selector, text):
        """向匹配 selector 的输入框写入文本（含 input/change 事件）。"""
        expr = self._el_expr(selector)
        js = (
            "(() => {"
            "  const el = " + expr + ";"
            "  if (!el) return 'NOT_FOUND';"
            "  el.focus();"
            "  el.value = " + json.dumps(text) + ";"
            "  el.dispatchEvent(new Event('input', {bubbles:true}));"
            "  el.dispatchEvent(new Event('change', {bubbles:true}));"
            "  return 'OK';"
            "})()"
        )
        res = self.send_cmd(
            "Runtime.evaluate", {"expression": js, "returnByValue": True}
        )
        val = res.get("result", {}).get("value")
        if val and str(val).startswith("NOT_FOUND"):
            raise RuntimeError(f"input_text 失败：{val}")
        return val

    def click_elem(self, selector):
        """点击匹配 selector 的元素（点击前等待元素出现）。"""
        # 等待元素出现，避免 open_url 后页面尚未就绪导致 NOT_FOUND
        deadline = time.time() + 8
        while time.time() < deadline:
            probe = self._probe(selector)
            if probe.get("found"):
                break
            time.sleep(0.3)
        expr = self._el_expr(selector)
        js = (
            "(() => {"
            "  const el = " + expr + ";"
            "  if (!el) return 'NOT_FOUND';"
            "  el.click();"
            "  return 'OK';"
            "})()"
        )
        res = self.send_cmd(
            "Runtime.evaluate", {"expression": js, "returnByValue": True}
        )
        val = res.get("result", {}).get("value")
        if val and str(val).startswith("NOT_FOUND"):
            raise RuntimeError(f"click_elem 失败：{val}")
        return val

    # ---------- 高级交互（录制器产出动作） ----------

    def _center(self, selector):
        """返回元素在视口中的中心坐标 {x, y}（CSS 像素）。"""
        p = self._probe(selector)
        if not p.get("found"):
            raise RuntimeError(f"_center 失败：未找到元素 {selector}")
        return {"x": p["x"], "y": p["y"]}

    def _scroll_into_view(self, selector):
        expr = self._el_expr(selector)
        js = (
            "(() => {"
            "  const el = " + expr + ";"
            "  if (!el) return 'NOT_FOUND';"
            "  el.scrollIntoView({block:'center', inline:'center'});"
            "  return 'OK';"
            "})()"
        )
        res = self.send_cmd(
            "Runtime.evaluate", {"expression": js, "returnByValue": True}
        )
        val = res.get("result", {}).get("value")
        if val and str(val).startswith("NOT_FOUND"):
            raise RuntimeError(f"_scroll_into_view 失败：{val}")

    def hover(self, selector):
        """悬停到元素（先滚动进视口，再派发 mouseMoved）。"""
        self._scroll_into_view(selector)
        c = self._center(selector)
        self.send_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": c["x"], "y": c["y"]},
        )
        return "OK"

    def drag(self, from_selector, to_selector, steps=12):
        """从一个元素拖拽到另一个元素（CDP 坐标级拖拽）。"""
        self._scroll_into_view(from_selector)
        self._scroll_into_view(to_selector)
        a = self._center(from_selector)
        b = self._center(to_selector)
        self.send_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": a["x"], "y": a["y"],
             "button": "left", "clickCount": 1},
        )
        # 分段移动，模拟真实拖拽轨迹
        for i in range(1, steps + 1):
            t = i / steps
            x = a["x"] + (b["x"] - a["x"]) * t
            y = a["y"] + (b["y"] - a["y"]) * t
            self.send_cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y},
            )
        self.send_cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": b["x"], "y": b["y"],
             "button": "left", "clickCount": 1},
        )
        return "OK"

    def upload_file(self, selector, file_paths):
        """向 file input 设置文件（file_paths 须为磁盘上的完整路径）。"""
        doc = self.send_cmd("DOM.getDocument", {"depth": -1})
        root = doc["root"]["nodeId"]
        node = self.send_cmd(
            "DOM.querySelector", {"nodeId": root, "selector": selector}
        )
        node_id = node.get("nodeId")
        if node_id is None:
            raise RuntimeError(f"upload_file 失败：未找到 {selector}")
        self.send_cmd(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": file_paths},
        )
        return "OK"

    # 特殊键（e.key -> CDP code）；其余单字符用 KeyX / DigitX 近似
    _SPECIAL_KEYS = {
        "Enter": "Enter", "Tab": "Tab", "Backspace": "Backspace",
        "Delete": "Delete", "Escape": "Escape", "Space": "Space",
        "ArrowUp": "ArrowUp", "ArrowDown": "ArrowDown",
        "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight",
        "Home": "Home", "End": "End", "PageUp": "PageUp", "PageDown": "PageDown",
        "Control": "Control", "Shift": "Shift", "Alt": "Alt", "Meta": "Meta",
    }
    for _i in range(1, 13):
        _SPECIAL_KEYS[f"F{_i}"] = f"F{_i}"

    def _key_tokens(self, token):
        """把录制器给的按键 token 转成 CDP key/code。"""
        if token in self._SPECIAL_KEYS:
            code = self._SPECIAL_KEYS[token]
            return token, code
        if len(token) == 1:
            if token.isalpha():
                return token, "Key" + token.upper()
            if token.isdigit():
                return token, "Digit" + token
            return token, token
        return token, token

    def _dispatch_key(self, token, event_type):
        key, code = self._key_tokens(token)
        self.send_cmd(
            "Input.dispatchKeyEvent",
            {"type": event_type, "key": key, "code": code},
        )

    def key_press(self, keys):
        """按键 / 组合键。keys 为 token 列表，如 ['Enter']、['Control','c']。"""
        keys = list(keys)
        if len(keys) == 1:
            self._dispatch_key(keys[0], "keyDown")
            self._dispatch_key(keys[0], "keyUp")
            return "OK"
        # 组合键：前面的作为修饰键按住，最后一个为主键
        for k in keys[:-1]:
            self._dispatch_key(k, "keyDown")
        self._dispatch_key(keys[-1], "keyDown")
        self._dispatch_key(keys[-1], "keyUp")
        for k in reversed(keys[:-1]):
            self._dispatch_key(k, "keyUp")
        return "OK"

    # ---------- 取证 ----------
    def screenshot(self, path="./screenshot.png"):
        """对当前标签页截图并保存为 PNG，返回文件路径。"""
        res = self.send_cmd("Page.captureScreenshot", {"format": "png"})
        data = res.get("data", "")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    # ---------- 清理 ----------
    def close(self):
        """断开 WebSocket 连接（不关闭用户标签页）。"""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None
