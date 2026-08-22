# -*- coding: utf-8 -*-
"""CLI / 代码执行器（编码即动作）。

对应 UFO² / CoAct-1 论文的核心发现：**把批量/数据类子任务交给 Python/Bash 执行，
比纯 GUI 点击更快更稳**（UFO² 步骤最多省 58.5%，CoAct-1 平均步骤 15->10）。

Action 形态：
    {"type":"cli","func":"run_python","args":["<code>"]}
    {"type":"cli","func":"run_bash",  "args":["<command>"]}
"""
import os
import sys
import json
import tempfile
import subprocess


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


def execute(action):
    """按 cli action 执行，返回结果 dict。"""
    func = action.func
    code = action.params[0] if action.params else ""
    if func == "run_python":
        return run_python(code)
    elif func == "run_bash":
        return run_bash(code)
    return {"rc": -3, "stdout": "", "stderr": "unknown cli func: %s" % func}
