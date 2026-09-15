# -*- coding: utf-8 -*-
"""访问本机服务（Chrome 调试端口等）用的 HTTP 客户端 —— 显式绕过系统代理。

## 为什么必须单独一个模块

Python 的 `urllib.request` 默认会读取系统代理设置（Windows 注册表的 ProxyServer，
以及 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量）。本机只要装了代理类软件（Clash 之类
的本地代理、公司网关、或者某些开发工具自己注入的 `HTTP_PROXY`），对
`http://127.0.0.1:9222/json` 的请求也会被送进代理，代理再返回
**502 Bad Gateway**。

表现得非常误导：Chrome 明明开着调试端口，程序却报 502，而不是「连接被拒」，
用户会以为是 Chrome 没启动或者技能坏了，实际是代理把回环地址也劫持了。

所以：**凡是对本机回环地址发起的 HTTP 请求，一律禁用代理。**

注意：T1 直连层调用的是业务系统的远程接口（金蝶/用友等），那些请求**需要**走
用户配置的网络（包括必要的代理），因此不受此模块影响，见 core/api_client.py。
"""
import urllib.request

# 单例 opener：ProxyHandler({}) 表示「任何协议都不使用代理」
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen_local(url, timeout=5, method=None, data=None, headers=None):
    """访问本机服务，绕过一切代理。

    url     : 完整地址，如 http://127.0.0.1:9222/json
    method  : 需要非 GET 时传（CDP 的 /json/new 要求 PUT）
    data    : 请求体（bytes）
    headers : 额外请求头
    """
    if method or data is not None or headers:
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return _local_opener.open(req, timeout=timeout)
    return _local_opener.open(url, timeout=timeout)


def describe_proxy_env():
    """把当前生效的代理配置整理成一行，便于把「502」这类怪错直接归因。"""
    parts = []
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY",
              "no_proxy"):
        import os
        v = os.environ.get(k)
        if v:
            parts.append("%s=%s" % (k, v))
    try:
        g = urllib.request.getproxies()
        if g:
            parts.append("system=%s" % {k: g[k] for k in sorted(g)})
    except Exception:
        pass
    return "；".join(parts) if parts else "（无代理配置）"
