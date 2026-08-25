# -*- coding: utf-8 -*-
"""API Client - T1 直连层 HTTP 客户端。

支持 HTTP 全方法 + 多种鉴权 + 超时重试 + 响应断言。
录制时由 network_capture 自动捕获 UI 点击背后的 API 请求，
回放时直接调用 API（T1），跳过脆弱的 GUI 定位。

Action 形态：
    {"type":"api","func":"call_api","args":["query_orders"],
     "credential_ref":"kingdee_token"}
"""
import json
import time
import logging

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from .credential_manager import resolve_credential

logger = logging.getLogger(__name__)


class ApiClient:
    """HTTP API 调用客户端。"""

    def __init__(self, registry=None):
        self.registry = registry or {}

    def call(self, api_name, credential_ref=None, overrides=None):
        """调用已注册的 API 模板。

        参数：
            api_name: api_registry 中注册的模板名
            credential_ref: 凭证引用（从 credential_manager 解析）
            overrides: 覆盖模板默认参数（path_params/query_params/body/headers）
        返回 {rc, status, headers, body, elapsed_ms}
        """
        if not _HAS_REQUESTS:
            return {"rc": -1, "error": "requests 未安装，请 pip install requests"}

        template = self.registry.get(api_name)
        if not template:
            return {"rc": -1, "error": "API 未注册: %s" % api_name}

        # 合并凭证与覆盖
        cred = resolve_credential(credential_ref) if credential_ref else {}
        ov = overrides or {}

        method = ov.get("method", template.get("method", "GET")).upper()
        url = self._build_url(template, ov, cred)
        headers = self._build_headers(template, ov, cred)
        params = ov.get("query_params", template.get("query_params", {}))
        body = ov.get("body", template.get("body"))
        timeout = ov.get("timeout", template.get("timeout", 30))
        max_retry = ov.get("max_retry", template.get("max_retry", 2))

        # 鉴权注入
        auth_type = template.get("auth", {}).get("type", "")
        self._inject_auth(headers, cred, template.get("auth", {}))

        t0 = time.time()
        last_err = None
        for attempt in range(max_retry + 1):
            try:
                resp = requests.request(
                    method, url,
                    headers=headers,
                    params=params if method == "GET" else None,
                    json=body if isinstance(body, (dict, list)) else None,
                    data=body if isinstance(body, str) else None,
                    timeout=timeout,
                )
                elapsed = int((time.time() - t0) * 1000)
                result = {
                    "rc": 0,
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": self._parse_body(resp),
                    "elapsed_ms": elapsed,
                    "url": resp.url,
                }
                # 断言检查
                assertions = template.get("assertions", [])
                for assertion in assertions:
                    if not self._check_assertion(result, assertion):
                        result["rc"] = 2
                        result["error"] = "断言失败: %s" % assertion.get("name", "")
                        return result
                logger.info("API %s %s -> %d (%dms)", method, url, resp.status_code, elapsed)
                return result
            except Exception as e:
                last_err = str(e)
                logger.warning("API 调用失败 (attempt %d/%d): %s", attempt + 1, max_retry + 1, e)
                if attempt < max_retry:
                    time.sleep(1.5 * (attempt + 1))
        return {"rc": -2, "error": "API 调用失败(%d次重试): %s" % (max_retry + 1, last_err)}

    def _build_url(self, template, ov, cred):
        base = template.get("base_url", "")
        path = ov.get("path", template.get("path", ""))
        # 路径参数替换
        path_params = ov.get("path_params", template.get("path_params", {}))
        for k, v in path_params.items():
            path = path.replace("{%s}" % k, str(v))
        return base.rstrip("/") + path if path else base

    def _build_headers(self, template, ov, cred):
        headers = dict(template.get("headers", {}))
        headers.update(ov.get("headers", {}))
        return headers

    def _inject_auth(self, headers, cred, auth_config):
        auth_type = auth_config.get("type", "")
        if auth_type == "bearer":
            token = cred.get("token") or cred.get("access_token", "")
            if token:
                headers["Authorization"] = "Bearer " + token
        elif auth_type == "basic":
            user = cred.get("username", "")
            pwd = cred.get("password", "")
            if user:
                import base64
                cred_str = "%s:%s" % (user, pwd)
                headers["Authorization"] = "Basic " + base64.b64encode(
                    cred_str.encode()).decode()
        elif auth_type == "api_key":
            key_name = auth_config.get("key_name", "X-API-Key")
            key_val = cred.get("api_key", "")
            if key_val:
                headers[key_name] = key_val
        elif auth_type == "cookie":
            cookie = cred.get("cookie", "")
            if cookie:
                headers["Cookie"] = cookie

    def _parse_body(self, resp):
        try:
            return resp.json()
        except Exception:
            return resp.text

    def _check_assertion(self, result, assertion):
        kind = assertion.get("type", "")
        if kind == "status":
            return result["status"] == assertion.get("expected", 200)
        if kind == "body_contains":
            body_str = json.dumps(result.get("body", ""))
            return assertion.get("expected", "") in body_str
        if kind == "body_path":
            path = assertion.get("path", "")
            expected = assertion.get("expected")
            actual = self._json_path(result.get("body"), path)
            return actual == expected
        if kind == "rc_zero":
            return result.get("rc") == 0
        return True

    @staticmethod
    def _json_path(data, path):
        """简易 JSON 路径：a.b.c -> data['a']['b']['c']。"""
        if not path or not data:
            return None
        cur = data
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                cur = cur[int(part)]
            else:
                return None
        return cur


def execute(action, registry=None, credential_ref=None):
    """按 api action 执行，返回结果 dict。"""
    client = ApiClient(registry=registry or {})
    api_name = action.params[0] if action.params else ""
    overrides = action.params[1] if len(action.params) > 1 else {}
    cred_ref = credential_ref or getattr(action, "credential_ref", None)
    return client.call(api_name, credential_ref=cred_ref, overrides=overrides)
