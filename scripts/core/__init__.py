# AutoPilot Composer —— 核心模块包
#
# 本包实现「原子动作建模 + 元素库 + CLI 执行器 + 复用组件 + 操作日志」的
# GUI / CLI / RPA 三合一底座，对应最新 OS Agent / Computer-Use 论文实践：
#   - UFO² (arXiv:2504.14603)   : GUI-API 混合 action layer，稳定定位优先可达性树
#   - CoAct-1 (arXiv:2508.03923) : 编码即动作（Python/Bash 执行器）
#   - OS-Copilot (FRIDAY)        : 自学习技能 / 复用组件
#   - Agent Behavior Mining      : 操作日志 -> process log（审计/流程挖掘）
#
# 设计原则：
#   1) 录制产物 = 原子动作（verb + element_ref + params），而非内联选择器
#   2) 元素定位走多策略定位器电池（id/name/placeholder/role_name/testid/css），
#      回放时按稳定性优先级依次尝试，失败自动回退
#   3) 高频小动作封装为参数化复用组件（components/）
#   4) 全程写结构化操作日志，支持审计与流程挖掘
