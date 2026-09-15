# -*- coding: utf-8 -*-
"""v3.6.0 JS 层验证入口：把 recorder.RECORDER_JS 交给 node 真实执行一遍。

Chrome 未开调试端口时，这是验证「注入页面的探测脚本」最接近真实的手段：
DOM 桩 + 合成事件 + 确定性定时器，不需要网络/GPU/浏览器。

跑法： python e2e_v360_js_test.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recorder  # noqa: E402

NODE_CANDIDATES = [
    r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
    r"C:\Program Files\nodejs\node.exe",
    "node",
]

js_tmp = os.path.join(HERE, "_recorder_injected.js")
test_js = os.path.join(HERE, "recorder_probe_js_test.js")


def find_node():
    for c in NODE_CANDIDATES:
        if c == "node":
            try:
                subprocess.run([c, "--version"], capture_output=True, timeout=10)
                return c
            except Exception:
                continue
        if os.path.exists(c):
            return c
    return None


def main():
    node = find_node()
    if not node:
        print("⚠️ 未找到 node，跳过 JS 层验证（不影响 Python 侧结论）")
        return 0

    with open(js_tmp, "w", encoding="utf-8") as f:
        f.write(recorder.RECORDER_JS)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([node, test_js, js_tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, cwd=HERE)
    print(r.stdout or "")
    if r.stderr:
        print("--- stderr ---")
        print(r.stderr[:3000])
    try:
        os.remove(js_tmp)
    except OSError:
        pass
    print("node 退出码:", r.returncode)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
