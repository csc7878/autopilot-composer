#!/usr/env python
# -*- coding: utf-8 -*-
r"""
AutoPilot Composer —— 网页操作录制器（Record 模式） v2

在你已用调试模式打开的 Chrome（默认 9222 端口）里正常操作网页，
本脚本实时捕获每一次操作，停止后一键导出三份可复用文件：

  1) task_flow.json   —— AutoPilot Composer 原生播放格式（main_task.py 直接跑）
                        每步携带 element_ref，回放走「元素库多策略定位」
  2) elements.json    —— 元素库（多策略定位器电池，改页面一处即可全局生效）
  3) recorded_flow.js —— 独立 Playwright 脚本（node recorded_flow.js 直接跑）

相比 v1，v2 新增录制动作：
  - 鼠标拖拽 drag（mousedown → move → mouseup）
  - 悬停 hover
  - 键盘 / 组合键（Enter / Tab / Ctrl+C 等，特殊键与组合键）
  - 文件上传 upload（<input type=file> 选中的文件名）
  - 跨域 iframe 内的操作（通过 addScriptToEvaluateOnNewDocument 注入到所有 frame）

前置条件（与 AutoPilot Composer 一致）：
  - Chrome 已用调试模式启动：
      "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
        --remote-debugging-port=9222 --remote-allow-origins=* ^
        --user-data-dir="C:\temp\apc_profile" --new-window
  - 至少已打开一个标签页

用法：
  python recorder.py                 # 交互录制：回车开始，输入任意内容回车停止
  python recorder.py --out tf.json --js flow.js --elements els.json
"""

import sys
import os
import json
import time
import threading
import argparse
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    raise SystemExit("缺少依赖 websocket-client，请先 pip install websocket-client")

# 接入 core 的元素库/Observer（不在则回退到纯 selector 模式）
try:
    from core.observer import Observer
    from core.element_repo import ElementRepository
    _HAS_CORE = True
except ImportError:
    _HAS_CORE = False


# ---------------------------------------------------------------------------
# 注入到网页里的录制脚本（监听用户操作，通过 CDP binding 回传）
# ---------------------------------------------------------------------------
RECORDER_JS = r"""
(function () {
  if (window.__apcRec) return;
  window.__apcRec = true;

  // 生成稳定 CSS 选择器：优先 id > name > 稳定属性 > 文本锚点 > 就近带 id 祖先 + 相对路径
  function bestSelector(el) {
    if (!el || el.nodeType !== 1) return 'body';
    var tag = el.tagName.toLowerCase();

    // 1) id 最稳
    if (el.id) return '#' + el.id;

    // 2) name 属性（表单元素常见）
    if (el.name) return tag + '[name="' + el.name + '"]';

    // 3) 其它稳定属性
    var stableAttr = ['placeholder', 'type', 'aria-label', 'data-testid', 'role', 'title', 'alt'];
    for (var ai = 0; ai < stableAttr.length; ai++) {
      var av = el.getAttribute(stableAttr[ai]);
      if (av) {
        // placeholder/aria-label/title 可能含空格，用属性选择器
        return tag + '[' + stableAttr[ai] + '="' + String(av).replace(/"/g, '\\"') + '"]';
      }
    }

    // 4) 按钮/链接的其它语义属性（标准 CSS，querySelectorAll 可直接用）
    if (tag === 'button' || tag === 'a') {
      if (el.getAttribute('type')) {
        return tag + '[type="' + el.getAttribute('type') + '"]';
      }
      var vv = el.getAttribute('value');
      if (vv) return tag + '[value="' + vv.replace(/"/g, '\\"') + '"]';
    }

    // 5) 向上找最近的带 id 祖先，从该祖先出发用最短相对路径（避免从 body 一路 nth-child）
    var anchor = null, node = el.parentNode;
    while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'body') {
      if (node.id) { anchor = node; break; }
      node = node.parentNode;
    }
    if (anchor) {
      // 从 anchor 出发，沿 child 链定位 el
      var chain = [];
      var cur = el;
      while (cur && cur !== anchor) {
        var s2 = cur.tagName.toLowerCase();
        var p2 = cur.parentNode;
        if (p2 && p2.nodeType === 1) {
          var k2 = 1;
          for (var j = 0; j < p2.children.length; j++) {
            if (p2.children[j].tagName.toLowerCase() === s2) {
              if (p2.children[j] === cur) break;
              k2++;
            }
          }
          s2 += ':nth-child(' + k2 + ')';
        }
        chain.unshift(s2);
        cur = p2;
      }
      return '#' + anchor.id + ' ' + chain.join(' > ');
    }

    // 6) 兜底：从 body 出发的 nth-child 链（最脆弱，仅作保底）
    var parts = [];
    var n2 = el;
    while (n2 && n2.nodeType === 1 && n2.tagName.toLowerCase() !== 'body') {
      var s3 = n2.tagName.toLowerCase();
      var pr = n2.parentNode;
      if (pr && pr.nodeType === 1) {
        var k3 = 1;
        for (var m = 0; m < pr.children.length; m++) {
          if (pr.children[m].tagName.toLowerCase() === s3) {
            if (pr.children[m] === n2) break;
            k3++;
          }
        }
        s3 += ':nth-child(' + k3 + ')';
        parts.unshift(s3);
      }
      n2 = pr;
    }
    if (!parts.length) return tag;
    return 'body > ' + parts.join(' > ');
  }

  function emit(obj) {
    obj.url = location.href;
    obj.ts = Date.now();
    obj.frame = (window !== window.top) ? location.href : null;
    try { apcRecord(JSON.stringify(obj)); } catch (e) { /* ignore */ }
  }

  function isSpecial(k) {
    return ['Enter','Tab','Backspace','Delete','Escape','Home','End','PageUp',
            'PageDown','Insert','ArrowUp','ArrowDown','ArrowLeft','ArrowRight']
            .indexOf(k) !== -1 || (/^F\d{ 1,2 }$/.test(k));
  }

  // -------- 首个页面作为第一段导航 --------
  emit({ type: 'navigate', initial: true });

  // -------- 点击 vs 拖拽 --------
  // 使用命名函数引用，便于重复注入时由 addEventListener 去重（同引用只绑一次），
  // 也让 __apcRec 重置后能干净地重新绑定。
  var downInfo = null;
  function onMouseDown(e) {
    if (e.button !== 0) return;
    downInfo = { x: e.clientX, y: e.clientY, el: e.target, t: Date.now() };
  }
  function clickPayload(el) {
    return {
      type: 'click',
      selector: bestSelector(el),
      tag: el.tagName ? el.tagName.toLowerCase() : '',
      text: (el.innerText || el.value || '').slice(0, 40),
      el_id: el.id || '',
      el_name: el.name || '',
      el_placeholder: el.placeholder || '',
      el_role: el.getAttribute('role') || '',
      el_aria_label: el.getAttribute('aria-label') || '',
      el_text: (el.innerText || '').slice(0, 40)
    };
  }
  function onMouseUp(e) {
    if (e.button !== 0) return;
    var el = e.target;
    if (!downInfo) {
      // mousedown 未捕获（合成事件 / 跨 context 丢失）时，仍兜底记为一次点击，
      // 避免「点了搜索按钮却录不到」——这正是程序化驱动与部分站点常见的丢事件根因。
      emit(clickPayload(el));
      return;
    }
    var dx = e.clientX - downInfo.x, dy = e.clientY - downInfo.y;
    var dist = Math.sqrt(dx * dx + dy * dy);
    if (dist > 12) {
      emit({
        type: 'drag',
        from_sel: bestSelector(downInfo.el),
        to_sel: bestSelector(el),
        from_x: downInfo.x, from_y: downInfo.y,
        to_x: e.clientX, to_y: e.clientY
      });
    } else {
      emit(clickPayload(el));
    }
    downInfo = null;
  }
  document.addEventListener('mousedown', onMouseDown);
  document.addEventListener('mouseup', onMouseUp);

  // -------- 悬停（指针在同一元素停留 > 600ms，且非拖拽） --------
  var hoverTimer = null, hoverEl = null;
  document.addEventListener('mousemove', function (e) {
    if (downInfo) return;  // 正在拖拽
    var el = e.target;
    if (el === hoverEl) return;
    hoverEl = el;
    if (hoverTimer) clearTimeout(hoverTimer);
    var sel = bestSelector(el);
    hoverTimer = setTimeout(function () { emit({ type: 'hover', selector: sel,
      el_id: el.id || '', el_name: el.name || '', el_placeholder: el.placeholder || '',
      el_role: el.getAttribute('role') || '', el_aria_label: el.getAttribute('aria-label') || '',
      el_text: (el.innerText || '').slice(0, 40),
      tag: el.tagName ? el.tagName.toLowerCase() : '' }); }, 600);
  });

  // -------- 输入 / 文件上传 --------
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (el.tagName === 'INPUT' && el.type === 'file') {
      var names = [];
      for (var i = 0; i < el.files.length; i++) names.push(el.files[i].name);
      emit({ type: 'upload', selector: bestSelector(el), files: names,
             el_id: el.id || '', el_name: el.name || '', el_placeholder: el.placeholder || '',
             el_role: el.getAttribute('role') || '', el_aria_label: el.getAttribute('aria-label') || '' });
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      emit({ type: 'change', selector: bestSelector(el), value: el.value,
             el_id: el.id || '', el_name: el.name || '', el_placeholder: el.placeholder || '',
             el_role: el.getAttribute('role') || '', el_aria_label: el.getAttribute('aria-label') || '',
             el_text: '', tag: el.tagName ? el.tagName.toLowerCase() : '' });
    }
  });

  // -------- 键盘：仅记录特殊键与组合键（普通字符由 change 捕获） --------
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) {
      var mods = [];
      if (e.shiftKey) mods.push('Shift');
      if (e.ctrlKey) mods.push('Control');
      if (e.metaKey) mods.push('Meta');
      if (e.altKey) mods.push('Alt');
      if (['Control','Shift','Alt','Meta'].indexOf(e.key) === -1) mods.push(e.key);
      emit({ type: 'keys', keys: mods });
      return;
    }
    if (isSpecial(e.key)) emit({ type: 'keys', keys: [e.key] });
  });
})();
"""


# ---------------------------------------------------------------------------
# 录制器本体
# ---------------------------------------------------------------------------
class WebRecorder:
    def __init__(self, port=9222):
        self.port = port
        self.http = "http://127.0.0.1:%d" % port
        self.ws = None
        self._id = 0
        self._running = False
        self._pending = {}
        self._pending_ev = threading.Event()
        self.collected = []
        # 用锁保护 _id / ws.send，避免后台 _reader_loop 与前台 send_cmd 并发竞争
        self._lock = threading.Lock()
        # 支持注入多个文档脚本（含跨 context 重注入），stop 时全部移除
        self._script_ids = []
        self.url_filter = None
        self._reader = None

    # ---- 连接 ----
    def connect(self, url_filter=None):
        """连接调试 Chrome 的一个标签页。

        url_filter：可选，传入子串/正则匹配的 URL，连到命中该模式的页面
        （多标签页场景下用于精准连到录制/回放目标，避免盲选第一个 page）。
        若未指定且没有命中项，则回退到第一个可用 page（兼容旧行为）。
        """
        if self.ws is not None:
            return self.ws
        if url_filter is not None:
            self.url_filter = url_filter
        with urllib.request.urlopen(self.http + "/json", timeout=5) as r:
            targets = json.load(r)
        cand = [t for t in targets
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not cand:
            cand = [t for t in targets if t.get("webSocketDebuggerUrl")]
        if self.url_filter:
            import re
            matched = [t for t in cand
                       if re.search(self.url_filter, t.get("url", "") or "")]
            if matched:
                cand = matched
        if not cand:
            raise RuntimeError("未找到可连接的浏览器标签页，请先在调试模式 Chrome 中打开页面")
        ws_url = cand[0]["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        return self.ws

    # ---- 后台收帧 ----
    def _reader_loop(self):
        while self._running:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if "id" in data and data["id"] in self._pending:
                with self._lock:
                    self._pending[data["id"]] = data
                self._pending_ev.set()
            elif data.get("method") == "Runtime.bindingCalled":
                try:
                    payload = json.loads(data["params"].get("payload", "{}"))
                    self.collected.append(payload)
                except Exception:
                    pass
            elif data.get("method") == "Runtime.executionContextCreated":
                # 关键修复：新 execution context（Page.navigate 跳转后必然触发）需要
                # 重建 apcRecord binding + 重新注入录制脚本，否则后续事件全部丢失。
                ctx = (data.get("params") or {}).get("context") or {}
                try:
                    self._activate_context(ctx)
                except Exception:
                    pass

    def _next_id(self):
        self._id += 1
        return self._id

    # ---- 发命令并等响应 ----
    def send_cmd(self, method, params=None, timeout=10):
        self.connect()
        with self._lock:
            mid = self._next_id()
            self._pending[mid] = None
            self._pending_ev.clear()
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._pending.get(mid) is not None:
                resp = self._pending[mid]
                if "error" in resp:
                    raise RuntimeError("CDP error on %s: %s" % (method, resp["error"]))
                return resp.get("result", {})
            time.sleep(0.05)
        raise TimeoutError("CDP 无响应: %s" % method)

    def _fire_cmd(self, method, params=None):
        """非阻塞发送（fire-and-forget），用于后台事件处理器，
        避免阻塞 _reader_loop 的 recv 循环导致死锁。不返回响应。"""
        try:
            with self._lock:
                mid = self._next_id()
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception:
            pass

    def _activate_context(self, ctx=None, blocking=False):
        """在新 / 当前 execution context 重建 apcRecord binding 并注入录制脚本。

        根因：Runtime.addBinding 是 execution-context 级别的。Page.navigate 或 SPA
        跳转创建新 context 后，旧 context 的 binding 失效，录制脚本的 emit 调用
        apcRecord 会静默失败（被 RECORDER_JS 的 try/catch 吞掉）——表现为「点了
        搜索按钮之后的事件全丢」。必须在每个新 context 重新 addBinding + 注入脚本。

        blocking=True 用于初始 context 的确定式激活（需等待响应生效）；
        非阻塞（默认）用于 _reader_loop 在 executionContextCreated 里调用，避免
        阻塞后台 recv 循环。
        """
        send = self.send_cmd if blocking else self._fire_cmd
        try:
            send("Runtime.addBinding", {"name": "apcRecord"})
        except Exception:
            pass
        # 注意：不要重置 window.__apcRec！RECORDER_JS 顶部的
        #   if (window.__apcRec) return;
        # 才是「同一 context 仅注入一次监听器」的去重机制。若这里把它置为
        # undefined，会导致 navigation 后脚本被重复注入、监听器翻倍、事件成对被录到。
        # addScriptToEvaluateOnNewDocument 已在新文档里注入过一次（设了 __apcRec），
        # 此处再 evaluate RECORDER_JS 会因 guard 提前 return，不会重复绑定。
        try:
            send("Runtime.evaluate", {"expression": RECORDER_JS, "returnByValue": True})
        except Exception:
            pass

    # ---- 开始 / 停止录制 ----
    def start(self):
        self.send_cmd("Runtime.enable")
        self.send_cmd("Page.enable")
        # 关键修复：不再把「addScriptToEvaluateOnNewDocument 自动注入」当作主通道
        # （它注入的脚本在新 context 里没有 binding，会静默丢事件）。改为：
        #  1) 初始 context 立即「阻塞式」激活，确保首屏录制立刻生效；
        #  2) 之后每个新 context 由 _reader_loop 的 executionContextCreated 事件
        #     异步重建 binding + 注入脚本（根治导航后事件丢失）。
        self._activate_context(blocking=True)
        # 保留 addScriptToEvaluateOnNewDocument 作为兜底（覆盖极少数未触发
        # executionContextCreated 的边界），并记录全部 id 以便 stop 时彻底移除。
        try:
            r = self.send_cmd("Page.addScriptToEvaluateOnNewDocument",
                              {"source": RECORDER_JS})
            self._script_ids.append(r.get("identifier"))
        except Exception:
            pass

    def stop(self):
        # 移除全部已添加的文档脚本，避免残留导致下次录制双监听 / 事件翻倍
        for sid in self._script_ids:
            try:
                self.send_cmd("Page.removeScriptToEvaluateOnNewDocument",
                              {"identifier": sid})
            except Exception:
                pass
        self._script_ids = []
        self._running = False
        time.sleep(0.2)
        return list(self.collected)

    def close(self):
        self._running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None


# ---------------------------------------------------------------------------
# 事件 → 可复用脚本 的转换
# ---------------------------------------------------------------------------
def _dedupe_navigate(events):
    """点击 / 按键引发的导航会与 open_url 重复，这里标出冗余导航。"""
    last_action_ts = 0
    for ev in events:
        t = ev.get("type")
        ts = ev.get("ts", 0)
        if t in ("click", "keys"):
            last_action_ts = max(last_action_ts, ts)
        elif t == "navigate":
            if not ev.get("initial") and (ts - last_action_ts) < 1500:
                ev["_redundant"] = True
            else:
                ev["_redundant"] = False
    return events


def events_to_taskflow(events, observer=None):
    """导出 AutoPilot Composer 原生 task_flow.json 步骤。

    若传入 observer（ElementRepository + 解析规则），则步骤会携带 element_ref，
    回放时走「元素库多策略定位」而非脆弱的原始 selector；否则保留旧行为（直接用
    录制时捕获的 selector）以兼容已有产物。
    """
    steps = []
    _dedupe_navigate(events)
    for ev in events:
        t = ev.get("type")
        if t == "navigate":
            if not ev.get("_redundant"):
                steps.append({"type": "browser", "func": "open_url", "args": [ev["url"]]})
        elif t == "click":
            step = {"type": "browser", "func": "click_elem", "args": [ev["selector"]]}
            _maybe_attach_ref(observer, ev, step)
            steps.append(step)
        elif t == "change":
            step = {"type": "browser", "func": "input_text",
                    "args": [ev["selector"], ev.get("value", "")]}
            _maybe_attach_ref(observer, ev, step)
            steps.append(step)
        elif t == "drag":
            steps.append({"type": "browser", "func": "drag",
                          "args": [ev["from_sel"], ev["to_sel"]]})
        elif t == "hover":
            step = {"type": "browser", "func": "hover", "args": [ev["selector"]]}
            _maybe_attach_ref(observer, ev, step)
            steps.append(step)
        elif t == "keys":
            steps.append({"type": "browser", "func": "key_press", "args": [ev["keys"]]})
        elif t == "upload":
            step = {"type": "browser", "func": "upload_file",
                    "args": [ev["selector"], ev.get("files", [])]}
            _maybe_attach_ref(observer, ev, step)
            steps.append(step)
    return steps


def _maybe_attach_ref(observer, ev, step):
    """尝试把元素登记进元素库，并在 step 上挂 element_ref（失败则静默保留 selector）。"""
    if not observer:
        return
    try:
        ref = observer._reg(ev, "web", name_hint=ev.get("tag") or step.get("func", "web"))
        if ref:
            step["element_ref"] = ref
    except Exception:
        pass


def events_to_playwright(events):
    """导出独立 Playwright 脚本的步骤行。"""
    lines = []
    _dedupe_navigate(events)
    for ev in events:
        t = ev.get("type")
        if t == "navigate":
            if not ev.get("_redundant"):
                lines.append("  await page.goto(%s);" % json.dumps(ev["url"]))
        elif t == "click":
            lines.append("  await page.click(%s);" % json.dumps(ev["selector"]))
        elif t == "change":
            lines.append("  await page.fill(%s, %s);"
                         % (json.dumps(ev["selector"]), json.dumps(ev.get("value", ""))))
        elif t == "drag":
            lines.append("  await page.dragAndDrop(%s, %s);"
                         % (json.dumps(ev["from_sel"]), json.dumps(ev["to_sel"])))
        elif t == "hover":
            lines.append("  await page.hover(%s);" % json.dumps(ev["selector"]))
        elif t == "keys":
            keys = ev["keys"]
            combo = "+".join(keys)
            lines.append("  await page.keyboard.press(%s);" % json.dumps(combo))
        elif t == "upload":
            lines.append("  await page.setInputFiles(%s, %s);"
                         % (json.dumps(ev["selector"]), json.dumps(ev.get("files", []))))
    return lines


PLAYWRIGHT_TEMPLATE = """// AutoPilot Composer 录制的 Playwright 脚本（独立运行，不依赖本技能）
// 运行前安装依赖： npm i playwright && npx playwright install chromium
// 运行： node recorded_flow.js
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
%s
  await browser.close();
})();
"""


def write_playwright(path, lines):
    body = "\n".join(lines) if lines else "  // （未录制到操作）"
    with open(path, "w", encoding="utf-8") as f:
        f.write(PLAYWRIGHT_TEMPLATE % body)


# ---------------------------------------------------------------------------
# 交互式录制器：逐操作弹窗确认 + 实时落盘（按域名归档）
# ---------------------------------------------------------------------------
class InteractiveRecorder:
    """在浏览器里实时捕获操作，每步都弹窗让用户确认是否记录，确认后实时写入
    SOP（task_flow.json）与元素库（elements.json）。产物按域名归档到 ./recordings/<host>/。

    用法：
      rec = InteractiveRecorder(port=9222)
      rec.connect()
      rec.run_loop()          # 阻塞，直到用户主动停止（Ctrl-C）或达到 max_steps
    """

    def __init__(self, port=9222, confirm=True, confirm_timeout=8, max_steps=None):
        self.port = port
        self.confirm = confirm
        self.confirm_timeout = confirm_timeout
        self.max_steps = max_steps
        self.rec = WebRecorder(port=port)
        self.home = os.path.dirname(os.path.abspath(__file__))
        self.workdir = None          # recordings/<host>/
        self.repo = None             # 用户元素库（非预置部分）
        self.preset_repo = None
        self.observer = None
        self.task_flow = []          # 已确认的步骤
        self._stop = False

    def prepare(self, first_url=None):
        """连接浏览器、注入录制脚本，并建立归档目录与元素库。"""
        self.rec.connect()
        self.rec.start()
        url = first_url or "（当前页面）"
        host = "default"
        if first_url and first_url.startswith("http"):
            from urllib.parse import urlparse
            host = urlparse(first_url).netloc or "default"
        self.workdir = os.path.join(self.home, "recordings", _safe_name(host))
        os.makedirs(self.workdir, exist_ok=True)
        elements_path = os.path.join(self.workdir, "elements.json")
        if _HAS_CORE:
            self.preset_repo = ElementRepository.load_preset(
                os.path.join(self.home, "preset_elements.json"))
            user_repo = ElementRepository(elements_path)
            self.repo = user_repo
            self.observer = Observer(user_repo, domain=host, preset_repo=self.preset_repo)
        return self.workdir

    def run_loop(self):
        """阻塞循环：逐条消费录制事件，弹窗确认后实时落盘。"""
        print("🎬 交互录制开始。在浏览器操作，每一步都会弹窗确认；Ctrl-C 停止。")
        try:
            while not self._stop:
                if self.max_steps and len(self.task_flow) >= self.max_steps:
                    break
                raw = self._wait_one_event()
                if raw is None:
                    continue
                self._handle_event(raw)
        except KeyboardInterrupt:
            pass
        self.rec.stop()
        self._save()
        print("✅ 录制结束，已记录 %d 步。产物目录：%s" % (len(self.task_flow), self.workdir))
        return self.workdir

    def _wait_one_event(self, timeout=0.5):
        """从录制器缓冲里取一条事件（带轮询）。"""
        start = time.time()
        while time.time() - start < timeout:
            if self.rec.collected:
                return self.rec.collected.pop(0)
            time.sleep(0.1)
        return None

    def _handle_event(self, ev):
        t = ev.get("type")
        # 过滤冗余导航
        if t == "navigate" and not ev.get("initial"):
            # 若紧跟在点击/按键后，视为冗余，跳过
            pass
        if t == "navigate" and ev.get("initial"):
            # 首屏导航：直接记录为 open_url（不弹窗，必记）
            step = {"type": "browser", "func": "open_url", "args": [ev["url"]]}
            self.task_flow.append(step)
            self._save()
            print("  📍 已自动记录导航:", ev["url"])
            return

        # 决定是否弹窗确认
        if not self.confirm:
            keep = True
        else:
            keep = confirm_action(ev, timeout=self.confirm_timeout)

        if not keep:
            print("  ⏭️  跳过:", _brief(ev))
            return

        step = {"type": "browser"}
        if t == "click":
            step.update({"func": "click_elem", "args": [ev["selector"]]})
            self._attach(ev, step)
        elif t == "change":
            step.update({"func": "input_text", "args": [ev["selector"], ev.get("value", "")]})
            self._attach(ev, step)
        elif t == "drag":
            step.update({"func": "drag", "args": [ev["from_sel"], ev["to_sel"]]})
        elif t == "hover":
            step.update({"func": "hover", "args": [ev["selector"]]})
            self._attach(ev, step)
        elif t == "keys":
            step.update({"func": "key_press", "args": [ev["keys"]]})
        elif t == "upload":
            step.update({"func": "upload_file", "args": [ev["selector"], ev.get("files", [])]})
            self._attach(ev, step)
        else:
            return

        self.task_flow.append(step)
        self._save()
        print("  ✓ 已记录:", _brief(ev))

    def _attach(self, ev, step):
        if self.observer:
            try:
                ref = self.observer._reg(ev, "web", name_hint=ev.get("tag") or step.get("func", "web"))
                if ref:
                    step["element_ref"] = ref
            except Exception:
                pass

    def _save(self):
        """实时写入 task_flow.json + elements.json。"""
        tf_path = os.path.join(self.workdir, "task_flow.json")
        with open(tf_path, "w", encoding="utf-8") as f:
            json.dump(self.task_flow, f, ensure_ascii=False, indent=2)
        if self.repo is not None:
            user_only = {k: v for k, v in self.repo.elements.items()
                         if not str(k).startswith("preset_")}
            self.repo.elements = user_only
            self.repo.save(os.path.join(self.workdir, "elements.json"))

    def stop(self):
        self._stop = True


def _safe_name(host):
    import re
    return re.sub(r"[^a-zA-Z0-9._-]", "_", host) or "default"


def _brief(ev):
    t = ev.get("type")
    if t == "click":
        return "点击 %s" % (ev.get("text") or ev.get("selector"))
    if t == "change":
        return "输入「%s」→ %s" % (ev.get("selector"), ev.get("value", ""))
    if t == "drag":
        return "拖拽 %s→%s" % (ev.get("from_sel"), ev.get("to_sel"))
    if t == "hover":
        return "悬停 %s" % ev.get("selector")
    if t == "keys":
        return "按键 %s" % ("+".join(ev.get("keys", [])))
    if t == "upload":
        return "上传 %s" % ", ".join(ev.get("files", []))
    return str(t)



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AutoPilot Composer 网页录制器 v2")
    ap.add_argument("--port", type=int, default=9222, help="Chrome CDP 端口")
    ap.add_argument("--url-filter", default=None,
                    help="按 URL 子串/正则筛选要连接的标签页（多标签页防误连目标）")
    ap.add_argument("--out", default="task_flow.json", help="导出的 task_flow.json 路径")
    ap.add_argument("--js", default="recorded_flow.js", help="导出的 Playwright JS 路径")
    ap.add_argument("--elements", default="elements.json", help="导出的元素库路径")
    ap.add_argument("--duration", type=float, default=None,
                    help="自动录制时长（秒），到时自动停止；无 tty / 非交互场景使用")
    ap.add_argument("--stop-file", default=None,
                    help="监控该文件出现即停止录制（无 tty 场景），如 .apc_stop")
    args = ap.parse_args()

    rec = WebRecorder(port=args.port)
    rec.connect(url_filter=args.url_filter)
    rec.start()

    # 停止信号：默认交互回车；无 tty 或指定 --duration / --stop-file 时自动降级，
    # 避免 input() 在无终端环境下立即 EOFError 退出（修复后台运行录制器秒退）。
    if (not sys.stdin.isatty()) or args.duration is not None or args.stop_file is not None:
        hints = []
        if args.duration is not None:
            hints.append("%d 秒后自动停止" % args.duration)
        if args.stop_file is not None:
            hints.append("检测到 %s 即停止" % args.stop_file)
        if not hints:
            hints.append("Ctrl-C 停止")
        print("🎬 非交互模式：录制中…（%s）" % "；".join(hints))
        try:
            if args.duration is not None:
                time.sleep(args.duration)
            else:
                while True:
                    if args.stop_file and os.path.exists(args.stop_file):
                        print("🛑 检测到停止文件，结束录制")
                        break
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        try:
            input("🔴 录制中… 在浏览器里正常操作，回到这里输入任意内容并回车停止：\n> ")
        except KeyboardInterrupt:
            pass

    events = rec.stop()
    rec.close()

    if not events:
        print("⚠️ 没有录制到任何操作。请确认 Chrome 是以调试模式打开、且页面已加载。")
        return

    # 元素库 + 原子动作建模（若 core 不可用则降级为纯 selector）
    observer = None
    preset_repo = None
    if _HAS_CORE:
        preset_repo = ElementRepository.load_preset(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "preset_elements.json")
        )
        user_repo = ElementRepository(args.elements)
        observer = Observer(user_repo, domain="web", preset_repo=preset_repo)

    tf = events_to_taskflow(events, observer)
    js_lines = events_to_playwright(events)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(tf, f, ensure_ascii=False, indent=2)
    write_playwright(args.js, js_lines)
    exported_elements = 0
    if observer is not None:
        # 只把「非预置」的元素落盘到用户元素库，避免重复
        user_only = {k: v for k, v in observer.repo.elements.items()
                     if not str(k).startswith("preset_")}
        user_repo.elements = user_only
        user_repo.save(args.elements)
        exported_elements = len(user_only)

    print("✅ 已录制 %d 个动作，导出文件：" % len(events))
    print("   - %s  （%d 步，可用 main_task.py 直接播放）" % (args.out, len(tf)))
    if exported_elements:
        print("   - %s  （%d 个元素，多策略定位器）" % (args.elements, exported_elements))
    print("   - %s  （node 直接运行，需 playwright）" % args.js)


if __name__ == "__main__":
    main()
