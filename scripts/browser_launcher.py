# -*- coding: utf-8 -*-
"""AutoPilot Composer —— 自带浏览器启动器（零下载、零体积）

思路：本机 Chrome / Chromium 本身就是开源 Chromium 的商业构建。与其额外
下载一个 Chromium（体积大、不易维护），不如直接「一键拉起」用户已安装的
Chrome，并开启远程调试端口。这样：

  - 无需用户手动在命令行敲一长串启动参数；
  - 每次拉起使用独立的 user-data-dir，互不污染日常浏览器配置；
  - 自动查找系统里的 Chrome / Chromium 可执行文件（Windows / macOS / Linux）；
  - 复用机制：若端口已存在可用调试实例，直接复用，不重复拉起。

用法：
  python browser_launcher.py            # 拉起并返回已就绪的端口（默认 9222）
  python browser_launcher.py --port 9333 --open https://www.baidu.com
"""
import os
import sys
import time
import json
import tempfile
import shutil
import subprocess
import urllib.request
import argparse


def find_chrome_executable():
    """跨平台查找 Chrome / Chromium 可执行文件。"""
    candidates = []
    # Windows
    if sys.platform.startswith("win"):
        base = os.environ.get("ProgramFiles", "C:\\Program Files")
        base_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(base_x86, "Google", "Chrome", "Application", "chrome.exe"),
            r"C:\Users\Administrator\.workbuddy\binaries\chrome\chrome.exe",
        ]
    elif sys.platform.startswith("darwin"):
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 兜底：PATH 中查找
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def port_alive(port):
    """端口是否已存在可用调试实例。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port,
                                    timeout=2) as r:
            data = json.load(r)
        return data.get("Browser") is not None
    except Exception:
        return False


def wait_port(port, timeout=15):
    """等待端口就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_alive(port):
            return True
        time.sleep(0.5)
    return False


def launch(port=9222, open_url=None, headless=False, profile_dir=None,
           reuse=True):
    """拉起（或复用）一个开启远程调试的 Chrome 实例。

    返回 dict：{ "new": bool, "port": int, "executable": str, "profile": str }
    """
    # 1) 若端口已可用且 reuse=True，直接复用
    if reuse and port_alive(port):
        exe = find_chrome_executable() or "（已有实例，无法探测路径）"
        return {"new": False, "port": port, "executable": exe, "profile": None}

    exe = find_chrome_executable()
    if not exe:
        raise RuntimeError(
            "未找到 Chrome/Chromium。请安装 Google Chrome，或把 chrome.exe 放到 "
            "C:\\Users\\Administrator\\.workbuddy\\binaries\\chrome\\chrome.exe"
        )

    # reuse=False 时若端口被占用，自动递增找一个空闲端口，避免旧实例冲突
    if not reuse:
        while port_alive(port):
            port += 1
            if port > 9999:
                raise RuntimeError("找不到可用调试端口（9222-9999 均被占用）")

    if profile_dir is None:
        # 用系统临时目录，避免项目路径下的锁定/权限问题
        base = os.path.join(tempfile.gettempdir(), "apc_chrome_profile")
        os.makedirs(base, exist_ok=True)
        profile_dir = os.path.join(base, "p_%d" % port)
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)

    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % profile_dir,
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
        args.append("--window-size=1280,900")
    if open_url:
        args.append(open_url)

    # 非阻塞后台启动
    if sys.platform.startswith("win"):
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP（后台分离，避免被父进程误回收）
        )
    else:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    if not wait_port(port, timeout=20):
        raise RuntimeError("Chrome 启动超时，未能在 %d 端口就绪" % port)
    return {"new": True, "port": port, "executable": exe, "profile": profile_dir}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AutoPilot Composer 浏览器启动器")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--open", default=None, help="启动后打开的 URL")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()

    info = launch(port=args.port, open_url=args.open,
                  headless=args.headless, profile_dir=args.profile)
    print(json.dumps(info, ensure_ascii=False))
