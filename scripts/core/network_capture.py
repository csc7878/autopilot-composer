# -*- coding: utf-8 -*-
"""Network Capture - CDP Network 域捕获，录制时自动生成 T1 API 动作。

录制用户操作网页时，同时监听 CDP Network 域：
  - 用户点击「登录」按钮 → CDP Input 域捕获 click 事件（T2）
  - 同时 Network 域捕获 POST /api/login 请求（T1）
  - Observer 合并两条动作，回放时 tier_resolver 先试 T1（直调 API），失败降级 T2

使用方式：在 WebRecorder.start() 时传入 NetworkCapture 实例。
"""
import json
import time
import logging

logger = logging.getLogger(__name__)

# 忽略的请求类型（静态资源不记为 API 动作）
_SKIP_RESOURCE_TYPES = {
    "Image", "Media", "Font", "Stylesheet", "Manifest", "Other"
}

# 忽略的 URL 模式（分析/追踪 SDK）
_SKIP_URL_PATTERNS = [
    "google-analytics.com",
    "googletagmanager.com",
    "hotjar.com",
    "sentry.io",
    "clarity.ms",
    "/track",
    "/analytics",
    "/log",
    "/beacon",
]


class NetworkCapture:
    """CDP Network 域捕获器，在录制期间收集 XHR/fetch 请求。"""

    def __init__(self):
        self.requests = []      # [{request_id, method, url, headers, post_data, ts, ...}]
        self._pending = {}      # request_id -> partial request
        self._enabled = False
        self._ws_send = None    # CDP send_cmd 函数引用

    def attach(self, send_cmd_fn):
        """附加到 CDP 连接，启用 Network 域监听。

        send_cmd_fn: recorder/cdp_engine 的 send_cmd 函数引用
        """
        self._ws_send = send_cmd_fn
        self._enabled = True
        try:
            self._ws_send("Network.enable", {})
            self._enabled = True
            logger.info("Network 域已启用，开始捕获 API 请求")
        except Exception as e:
            logger.warning("启用 Network 域失败: %s", e)
            self._enabled = False

    def detach(self):
        """停止监听。"""
        if self._ws_send and self._enabled:
            try:
                self._ws_send("Network.disable", {})
            except Exception:
                pass
        self._enabled = False

    def handle_event(self, method, params):
        """处理 CDP Network 域事件（由 recorder 的 _reader_loop 调用）。

        需要处理的事件：
        - Network.requestWillBeSent  -> 记录请求开始
        - Network.responseReceived   -> 记录响应状态
        - Network.loadingFinished    -> 记录响应体（可选）
        """
        if not self._enabled:
            return

        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            req_id = params.get("requestId", "")
            url = req.get("url", "")

            # 过滤静态资源和分析追踪
            if self._should_skip(params):
                return

            self._pending[req_id] = {
                "request_id": req_id,
                "method": req.get("method", "GET"),
                "url": url,
                "headers": req.get("headers", {}),
                "post_data": req.get("postData", ""),
                "ts": params.get("timestamp", 0) * 1000,
                "type": params.get("type", ""),
                "initiator": params.get("initiator", {}),
            }

        elif method == "Network.responseReceived":
            req_id = params.get("requestId", "")
            if req_id in self._pending:
                resp = params.get("response", {})
                self._pending[req_id]["status"] = resp.get("status", 0)
                self._pending[req_id]["response_headers"] = resp.get("headers", {})
                self._pending[req_id]["mime_type"] = resp.get("mimeType", "")

        elif method == "Network.loadingFinished":
            req_id = params.get("requestId", "")
            if req_id in self._pending:
                req = self._pending.pop(req_id)
                # 补全时间戳
                if not req.get("ts"):
                    req["ts"] = int(time.time() * 1000)
                self.requests.append(req)

    def _should_skip(self, params):
        """判断该请求是否应跳过（静态资源/分析追踪）。"""
        rtype = params.get("type", "")
        if rtype in _SKIP_RESOURCE_TYPES:
            return True

        url = params.get("request", {}).get("url", "").lower()
        for pattern in _SKIP_URL_PATTERNS:
            if pattern in url:
                return True

        # 跳过 OPTIONS 预检请求
        method = params.get("request", {}).get("method", "")
        if method == "OPTIONS":
            return True

        return False

    def get_api_events(self):
        """返回捕获的 API 请求列表，按时间排序。"""
        return sorted(self.requests, key=lambda r: r.get("ts", 0))

    def to_api_templates(self, api_registry=None):
        """把捕获的请求转为 API 注册表模板。

        只记录 XHR/fetch 类型的请求（type 为 XHR/Fetch）。
        返回 {api_name: template} 字典。
        """
        from .api_registry import ApiRegistry
        if api_registry is None:
            api_registry = ApiRegistry()

        for req in self.requests:
            # 只录 XHR/fetch（真正的 API 调用）
            rtype = req.get("type", "")
            if rtype not in ("XHR", "Fetch"):
                continue

            method = req.get("method", "GET")
            url = req.get("url", "")
            name = api_registry.infer_name_from_url(method, url)

            # 从 URL 拆分 base_url 和 path
            from urllib.parse import urlparse
            parsed = urlparse(url)
            base_url = "%s://%s" % (parsed.scheme, parsed.netloc)
            path = parsed.path

            template = {
                "method": method,
                "base_url": base_url,
                "path": path,
                "headers": dict(req.get("headers", {})),
                "auth": {"type": "auto"},  # 回放时从 credential_ref 注入
                "timeout": 30,
                "max_retry": 1,
                "assertions": [
                    {"type": "status", "expected": req.get("status", 200)},
                ],
            }
            if req.get("post_data"):
                try:
                    template["body"] = json.loads(req["post_data"])
                except Exception:
                    template["body"] = req["post_data"]

            # 避免重名：若已存在同名，加序号
            final_name = name
            idx = 2
            while final_name in api_registry.templates:
                final_name = "%s_%d" % (name, idx)
                idx += 1
            api_registry.register(final_name, template)

        return api_registry
