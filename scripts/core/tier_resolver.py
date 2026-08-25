# -*- coding: utf-8 -*-
"""Tier Resolver - T1 直连层择优执行与自动降级。

四层自动化模型：
    T1 api/cli/db   - 直调 API/CLI/SQL（最快最稳，不受 UI 改版影响）
    T2 cdp_element  - 浏览器 CDP 元素定位（稳定，抗改版）
    T3 uia_element  - 桌面 UIA 元素定位（较稳定）
    T4 coord        - 屏幕坐标（最脆弱，兜底）

回放策略：每步先检查是否有 T1 注册表条目，有则先试 T1；
T1 成功则跳过 T2/T3/T4；T1 失败自动降级到 T2，再失败到 T3/T4。

这样录制时用户点击「登录」按钮，Network 域同时捕获了 POST /api/login，
回放时直接调用 API（一步完成），而非靠脆弱的 CSS 选择器找按钮再点击。
"""
import logging
from .actions import Action

logger = logging.getLogger(__name__)

# Tier 优先级
TIER_PRIORITY = ["api", "cli", "sql", "browser", "gui", "component"]


class TierResolver:
    """每步检查是否有 T1 路径可走，择优执行，失败自动降级。"""

    def __init__(self, api_registry=None, cli_registry=None, db_registry=None):
        self.api_registry = api_registry or {}
        self.cli_registry = cli_registry or {}
        self.db_registry = db_registry or {}

    def has_t1(self, step):
        """检查该步骤是否有 T1 直连路径可用。

        一个步骤如果有 T1 路径，意味着：
        - type 为 api/cli/sql（本身就是 T1）
        - 或 type 为 browser/gui，但携带 t1_ref 指向 api/cli 模板
        """
        step_type = step.get("type", "")
        if step_type in ("api", "cli", "sql"):
            return True
        # 检查是否有 t1_ref（录制时 Network 捕获的关联 API）
        t1_ref = step.get("t1_ref")
        if t1_ref:
            return True
        return False

    def resolve_t1(self, step):
        """解析出 T1 动作（若存在）。

        对于 browser/gui 步骤，若录制时 Network 捕获了关联 API，
        step 中会携带 t1_ref 字段指向 API 模板名。
        """
        step_type = step.get("type", "")

        # 本身就是 T1
        if step_type in ("api", "cli", "sql"):
            return Action.from_dict(step)

        # browser/gui 步骤的关联 T1
        t1_ref = step.get("t1_ref")
        if t1_ref and t1_ref.get("type") == "api":
            api_name = t1_ref.get("name", "")
            if api_name and self.api_registry.get(api_name):
                return Action(
                    type="api", func="call_api",
                    params=[api_name, t1_ref.get("overrides", {})],
                    credential_ref=t1_ref.get("credential_ref"),
                    note="T1: " + api_name,
                )
        if t1_ref and t1_ref.get("type") == "cli":
            template_name = t1_ref.get("name", "")
            if template_name and self.cli_registry.get(template_name):
                return Action(
                    type="cli", func="run_template",
                    params=[template_name, t1_ref.get("params", {})],
                    note="T1: " + template_name,
                )

        return None

    def should_fallback(self, t1_result):
        """判断 T1 执行结果是否需要降级到 T2/T3/T4。"""
        if not t1_result:
            return True
        rc = t1_result.get("rc", -1)
        if rc != 0:
            return True
        # API 返回非 2xx 状态也降级
        status = t1_result.get("status", 0)
        if status and not (200 <= status < 300):
            return True
        return False

    def describe_tier(self, step):
        """返回该步骤的 tier 标注（用于回放日志）。"""
        if self.has_t1(step):
            t1 = self.resolve_t1(step)
            if t1:
                return "T1(%s)" % t1.func
        step_type = step.get("type", "")
        if step_type == "browser":
            return "T2(cdp)"
        if step_type == "gui":
            ref = step.get("element_ref")
            return "T3(uia)" if ref else "T4(coord)"
        return "T?"
