# -*- coding: utf-8 -*-
"""DB Security - T1 直连层 SQL 安全防护。

防注入（参数化查询）、read_only 保护、审计日志、敏感字段脱敏。
"""
import re
import time
import json
import os
import hashlib

# 审计日志路径
_AUDIT_LOG_PATH = None

# SQL 注入危险模式
_DANGEROUS_PATTERNS = [
    r";\s*DROP\s+TABLE",
    r";\s*DELETE\s+FROM\s+\w+\s*;?\s*$",
    r"--\s*$",
    r"/\*.*\*/",
    r"xp_cmdshell",
    r"sp_executesql\s+'[^']*'\s*\+",
]

_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]


def check_sql_injection(sql, params=None):
    """检查 SQL 是否有疑似注入风险。返回 True=安全。"""
    if not sql:
        return True
    for pattern in _PATTERNS_COMPILED:
        if pattern.search(sql):
            return False
    # 如果有参数但 SQL 里有字符串拼接（{}% format），也标记
    if params and ("{" in sql or "%s" in sql.replace("%s", "")):
        if sql.count("'") > 2:
            return False
    return True


def mask_sensitive(rows, sensitive_fields):
    """对查询结果中的敏感字段脱敏。"""
    if not sensitive_fields or not rows:
        return rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in sensitive_fields:
            if field in row and row[field]:
                val = str(row[field])
                if len(val) <= 4:
                    row[field] = "*" * len(val)
                else:
                    row[field] = val[:2] + "*" * (len(val) - 4) + val[-2:]
    return rows


def audit_log(db_type, operation, sql, params, elapsed_ms, affected):
    """记录 SQL 审计日志。"""
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is None:
        _AUDIT_LOG_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "db_audit.log"
        )
    entry = {
        "ts": int(time.time() * 1000),
        "db_type": db_type,
        "operation": operation,
        "sql_preview": sql[:200] if sql else "",
        "has_params": bool(params),
        "elapsed_ms": elapsed_ms,
        "rows_affected": affected,
        "sql_hash": hashlib.md5((sql or "").encode("utf-8")).hexdigest()[:8],
    }
    try:
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def set_audit_path(path):
    global _AUDIT_LOG_PATH
    _AUDIT_LOG_PATH = path
