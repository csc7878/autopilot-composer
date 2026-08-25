# -*- coding: utf-8 -*-
"""CLI / 代码执行器（编码即动作）- v3.4.0 扩展。

支持四类执行方式：
  1) subprocess  - Python / Bash（原有，编码即动作）
  2) com         - COM 自动化（WPS/金蝶/Excel 等 Windows COM 对象）
  3) powershell  - PowerShell 脚本（系统管理/文件操作/注册表）
  4) sdk         - SDK 调用（通过注册表模板，白名单模式）

对应 UFO² / CoAct-1 论文：把批量/数据类子任务交给代码执行，
比纯 GUI 点击更快更稳（UFO² 步骤最多省 58.5%，CoAct-1 平均步骤 15->10）。

Action 形态：
    {"type":"cli","func":"run_python","args":["<code>"]}
    {"type":"cli","func":"run_bash",  "args":["<command>"]}
    {"type":"cli","func":"run_com",   "args":["export_excel","{sheet:'Sheet1'}"]}
    {"type":"cli","func":"run_ps",    "args":["Get-Process | Select-Object -First 5"]}
    {"type":"cli","func":"run_template","args":["export_kingdee_report","{report_type:'monthly'}"]}
"""
import os
import sys
import json
import tempfile
import subprocess
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1) subprocess: Python / Bash
# ---------------------------------------------------------------------------

def run_python(code, timeout=120, cwd=None):
    """用当前 Python 解释器执行一段代码字符串，返回 {rc,stdout,stderr}。"""
    py = sys.executable
    fd, path = tempfile.mkstemp(suffix=".py", prefix="apc_cli_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run([py, path], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "stdout": "", "stderr": "timeout(%ss)" % timeout}
    except Exception as e:
        return {"rc": -2, "stdout": "", "stderr": "exception: %s" % e}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def run_bash(cmd, timeout=120, cwd=None):
    """执行一条 shell 命令（Windows 用 cmd /c，类 Unix 用 bash -c）。"""
    shell = ["cmd", "/c"] if os.name == "nt" else ["bash", "-c"]
    try:
        r = subprocess.run(shell + [cmd], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "stdout": "", "stderr": "timeout(%ss)" % timeout}
    except Exception as e:
        return {"rc": -2, "stdout": "", "stderr": "exception: %s" % e}


# ---------------------------------------------------------------------------
# 2) COM 自动化（WPS / 金蝶 / Excel 等）
# ---------------------------------------------------------------------------

def run_com(prog_id, method, args=None, timeout=120):
    """通过 COM 接口控制 Windows 应用程序。

    prog_id: COM 对象标识（如 "Excel.Application" / "KWPS.Application" / "Kingdee.K3Cloud"）
    method: 要调用的方法或属性路径（如 "Workbooks.Open" / "ActiveWorkbook.SaveAs"）
    args: 方法参数列表

    返回 {rc, result, stdout, stderr}
    """
    if os.name != "nt":
        return {"rc": -1, "stdout": "", "stderr": "COM 仅支持 Windows",
                "result": None}
    try:
        import win32com.client
    except ImportError:
        return {"rc": -1, "stdout": "", "stderr": "pywin32 未安装",
                "result": None}

    try:
        app = win32com.client.Dispatch(prog_id)
        # 沿属性/方法路径深入：a.b.c -> getattr(getattr(app, 'a'), 'b').c()
        obj = app
        parts = method.split(".")
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                # 最后一段是方法调用
                if args:
                    result = getattr(obj, part)(*args)
                else:
                    result = getattr(obj, part)()
            else:
                obj = getattr(obj, part)

        # 尝试关闭应用
        try:
            if hasattr(app, "Quit"):
                app.Quit()
        except Exception:
            pass

        return {"rc": 0, "result": result if not isinstance(result,
                (object,)) else str(result),
                "stdout": "", "stderr": ""}
    except Exception as e:
        return {"rc": -2, "result": None, "stdout": "",
                "stderr": "COM 调用失败(%s.%s): %s" % (prog_id, method, e)}


# ---------------------------------------------------------------------------
# 3) PowerShell
# ---------------------------------------------------------------------------

def run_powershell(script, timeout=120):
    """执行 PowerShell 脚本。

    Windows 10+ 自带 powershell.exe，无需额外安装。
    适合系统管理、文件批量操作、注册表修改等场景。

    返回 {rc, stdout, stderr}
    """
    if os.name != "nt":
        return {"rc": -1, "stdout": "", "stderr": "PowerShell 仅支持 Windows"}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8",
        )
        return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "stdout": "", "stderr": "timeout(%ss)" % timeout}
    except Exception as e:
        return {"rc": -2, "stdout": "", "stderr": "exception: %s" % e}


# ---------------------------------------------------------------------------
# 4) 模板执行（白名单模式）
# ---------------------------------------------------------------------------

def run_template(template_name, params=None, registry=None):
    """执行注册表中已注册的 CLI 模板（白名单模式，安全）。

    template_name: cli_registry 中的模板名
    params: 模板参数 dict
    """
    if registry is None:
        from .cli_registry import get_registry
        registry = get_registry()

    try:
        template, rendered = registry.render(template_name, params)
    except RuntimeError as e:
        return {"rc": -1, "stdout": "", "stderr": str(e)}

    executor = template.get("executor", "subprocess")
    timeout = template.get("timeout", 120)
    cwd = template.get("cwd")

    if executor == "subprocess":
        return run_bash(rendered, timeout=timeout, cwd=cwd)
    elif executor == "powershell":
        return run_powershell(rendered, timeout=timeout)
    elif executor == "com":
        # COM 模板：command 格式为 "ProgID|method"
        parts = rendered.split("|", 1)
        if len(parts) != 2:
            return {"rc": -1, "stdout": "",
                    "stderr": "COM 模板格式错误，需 ProgID|method"}
        return run_com(parts[0].strip(), parts[1].strip(),
                        args=list(params.values()) if params else None,
                        timeout=timeout)
    elif executor == "sdk":
        # SDK 模板：通过 Python 代码调用
        return run_python(rendered, timeout=timeout, cwd=cwd)
    else:
        return {"rc": -3, "stdout": "",
                "stderr": "unknown executor: %s" % executor}


# ---------------------------------------------------------------------------
# 统一执行入口
# ---------------------------------------------------------------------------

def execute(action, registry=None):
    """按 cli action 执行，返回结果 dict。"""
    func = action.func
    code = action.params[0] if action.params else ""
    if func == "run_python":
        return run_python(code)
    elif func == "run_bash":
        return run_bash(code)
    elif func == "run_com":
        # args: [prog_id, method, [arg1, arg2, ...]]
        prog_id = action.params[0] if len(action.params) > 0 else ""
        method = action.params[1] if len(action.params) > 1 else ""
        com_args = action.params[2] if len(action.params) > 2 else None
        return run_com(prog_id, method, args=com_args)
    elif func == "run_ps":
        return run_powershell(code)
    elif func == "run_template":
        params = action.params[1] if len(action.params) > 1 else {}
        return run_template(code, params=params, registry=registry)
    return {"rc": -3, "stdout": "", "stderr": "unknown cli func: %s" % func}
