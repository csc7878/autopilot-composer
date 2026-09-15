# -*- coding: utf-8 -*-
"""环境守卫（Environment Guard）——检测并尝试自愈「环境劫持」。

理论来源
--------
- OS-Kairos（arXiv:2503.16465）把 environment hijacks（弹窗劫持、网络中断、
  权限请求）列为 GUI 智能体三大复杂场景之一；
- Caution for the Environment: Multimodal Agents are Susceptible to
  Environmental Distractions（见 dive-into-llms 第9章 slides p30）指出
  环境中的干扰性信息会显著破坏智能体可靠性。

对确定性 RPA 来说，环境异常不表现为「模型幻觉」，而是**可观测的确定性事实**，
而且后果比模型场景更致命：一旦被踢回登录页，后续几十步都会在错误的页面上
「假装成功」地空点——不报错、不中断，直到最后一步才对不上。这正是最隐蔽
的故障模式。

本模块检测的 4 类环境异常：
  cdp_down   浏览器调试通道断了 / 没有可用标签页
  login      被跳转到登录页（会话过期）——最隐蔽、危害最大
  error_page 跳到 404/500 之类的错误页
  dialog     出现意料之外的模态弹窗，遮住目标元素

零模型、零截图识别：只用 URL 字符串 + 一次 Runtime.evaluate 做 DOM 特征探测。
刻意分两档调用以平衡开销与准确：
  - 执行前用「浅检」（只看 URL，0~1 次往返，零误报）
  - 失败诊断时用「深检」（额外探测弹窗与密码框，误报只影响诊断文案）
"""
import time

DEFAULT_LOGIN_PATTERNS = ["/login", "/signin", "/sign-in", "/auth/", "/sso",
                          "login?", "signin?", "passport", "oauth"]
DEFAULT_ERROR_PATTERNS = ["/404", "/500", "/502", "/503", "error.html",
                          "errorpage", "notfound", "not-found"]

# 深层探测：一次 JS 调用取回弹窗 / 密码框 / 标题
_DEEP_PROBE_JS = r"""
(() => {
  const vis = (el) => {
    if (!el) return false;
    try {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      return r.width > 30 && r.height > 20;
    } catch (e) { return false; }
  };
  const sels = ['[role=dialog]', '[aria-modal="true"]', '.el-dialog__wrapper',
                '.ant-modal-wrap', '.van-dialog', '.layui-layer', '.modal.show',
                '.swal2-popup'];
  let dlg = '';
  for (const s of sels) {
    const el = document.querySelector(s);
    if (vis(el)) {
      dlg = ((el.innerText || '').replace(/\s+/g, ' ')).slice(0, 120);
      break;
    }
  }
  return {
    url: location.href,
    title: document.title,
    dialog: dlg,
    password_input: !!document.querySelector('input[type=password]')
  };
})()
"""


class EnvironmentGuard:
    def __init__(self, cfg=None):
        c = cfg or {}
        self.enabled = bool(c.get("enabled", True))
        self.login_patterns = [p.lower() for p in
                               (c.get("login_patterns") or DEFAULT_LOGIN_PATTERNS)]
        self.error_patterns = [p.lower() for p in
                               (c.get("error_patterns") or DEFAULT_ERROR_PATTERNS)]
        self.last = None

    # ---------------- 检测 ----------------

    def check(self, browser=None, deep=False):
        """检测当前环境状态。

        返回 {"kind": "ok"|"cdp_down"|"login"|"error_page"|"dialog",
              "detail": 人类可读说明, "url":..., "title":..., "skipped": bool}
        """
        if not self.enabled:
            return self._ret({"kind": "ok", "detail": "", "skipped": True})
        if browser is None:
            return self._ret({"kind": "ok", "skipped": True,
                              "detail": "未接入浏览器（略过环境检测）"})
        try:
            url = browser.current_url()
        except Exception as e:
            return self._ret({"kind": "cdp_down",
                              "detail": "无法读取页面地址：%s" % str(e)[:120]})
        if not url:
            return self._ret({"kind": "cdp_down",
                              "detail": "浏览器调试通道不可用，或没有可操作的标签页"})

        low = url.lower()
        hit_login = next((p for p in self.login_patterns if p in low), None)
        hit_error = next((p for p in self.error_patterns if p in low), None)

        info = {}
        if deep or hit_login or hit_error:
            info = self._deep_probe(browser)

        title = info.get("title") or ""
        dlg = info.get("dialog") or ""

        # 弹窗优先于页面类型：弹窗会遮住一切，是最直接的风险
        if dlg:
            return self._ret({"kind": "dialog", "url": url, "title": title,
                              "dialog_text": dlg,
                              "detail": "弹窗遮挡：「%s」" % dlg[:60]})

        # 登录页：只有在「深检确认有密码框」或深检未执行时才判定，
        # 避免把正常停留在 /login 展示页的场景误判
        if hit_login:
            if not deep or info.get("password_input"):
                return self._ret({"kind": "login", "url": url, "title": title,
                                  "detail": "URL 命中登录特征「%s」，页面存在密码输入框"
                                            % hit_login})

        if hit_error:
            return self._ret({"kind": "error_page", "url": url, "title": title,
                              "detail": "URL 命中错误页特征「%s」%s"
                                        % (hit_error, ("，标题：" + title) if title else "")})

        if title and any(k in title for k in ("无法访问", "找不到", "连接失败",
                                              "404", "500", "Access Denied")):
            return self._ret({"kind": "error_page", "url": url, "title": title,
                              "detail": "页面标题像错误页：%s" % title})

        return self._ret({"kind": "ok", "url": url, "title": title, "detail": ""})

    def _deep_probe(self, browser):
        try:
            res = browser.send_cmd("Runtime.evaluate",
                                   {"expression": _DEEP_PROBE_JS, "returnByValue": True})
            return res.get("result", {}).get("value") or {}
        except Exception:
            return {}

    def _ret(self, d):
        self.last = d
        return d

    # ---------------- 自愈 ----------------

    def recover(self, browser=None, kind=None, checkpoint=None):
        """针对已知环境异常尝试轻量自愈。

        返回 {"ok": bool, "action": str, "detail": str}
        注意：登录页 expire 无法自动恢复（需要人工重新登录），会明确返回 False，
        而不是盲目跳过——这正是 OS-Kairos 强调的「该喊人时就喊人」。
        """
        if not self.enabled or browser is None:
            return {"ok": False, "action": "none", "detail": "环境守卫未启用"}

        if kind == "dialog":
            try:
                browser.key_press(["Escape"])
                time.sleep(0.4)
                if self.check(browser, deep=True).get("kind") != "dialog":
                    return {"ok": True, "action": "按 Esc 关闭弹窗",
                            "detail": "弹窗已关闭"}
                # Esc 无效（很多弹窗不吃 Esc）：改用点击遮罩/关闭按钮
                js = ("(() => { const b = document.querySelector("
                      "'.el-dialog__headerbtn,.ant-modal-close,.layui-layer-close,"
                      "[role=dialog] [aria-label=Close]');"
                      " if (b) { b.click(); return true; } return false; })()")
                res = browser.send_cmd("Runtime.evaluate",
                                       {"expression": js, "returnByValue": True})
                time.sleep(0.3)
                if res.get("result", {}).get("value"):
                    if self.check(browser, deep=True).get("kind") != "dialog":
                        return {"ok": True, "action": "点击弹窗关闭按钮",
                                "detail": "弹窗已关闭"}
            except Exception as e:
                return {"ok": False, "action": "关闭弹窗", "detail": str(e)[:120]}
            return {"ok": False, "action": "关闭弹窗",
                    "detail": "弹窗仍在，需人工处理（可能是需要填写的业务弹窗）"}

        if kind == "error_page":
            if checkpoint and checkpoint.get("url"):
                try:
                    browser.open_url(checkpoint["url"])
                    st = self.check(browser, deep=True)
                    if st.get("kind") == "ok":
                        return {"ok": True, "action": "回滚到上一个正常页面",
                                "detail": checkpoint["url"][:100]}
                except Exception as e:
                    return {"ok": False, "action": "回滚页面", "detail": str(e)[:120]}
            return {"ok": False, "action": "回滚页面",
                    "detail": "没有可用的检查点，需人工确认页面状态"}

        if kind == "cdp_down":
            try:
                if hasattr(browser, "connect"):
                    browser.connect()
                if self.check(browser, deep=True).get("kind") != "cdp_down":
                    return {"ok": True, "action": "重连浏览器调试通道", "detail": "已恢复"}
            except Exception as e:
                return {"ok": False, "action": "重连浏览器", "detail": str(e)[:120]}
            return {"ok": False, "action": "重连浏览器",
                    "detail": "重连失败，请确认 Chrome 是否以调试模式运行（端口 9222）"}

        if kind == "login":
            return {"ok": False, "action": "需要人工登录",
                    "detail": "会话已过期被踢回登录页，自动重登涉及凭证，请人工登录后 "
                              "用 `python main_task.py` 从断点续跑"}

        return {"ok": False, "action": "none", "detail": "无需恢复或未知异常类型"}


def build_from_config(cfg):
    return EnvironmentGuard(cfg or {})
