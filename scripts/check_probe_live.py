# -*- coding: utf-8 -*-
r"""真机自检：录制器的「动作后探测」在真实 Chrome 里是否正常工作。

## 什么时候用它

`e2e_v360_test.py` + `e2e_v360_js_test.py` 已经用合成数据与 DOM 桩覆盖了推断逻辑和
注入脚本逻辑。但有一件事它们证明不了：**在真实浏览器 + 真实页面里**，注入脚本的
定时器、跨 context、元素回读是否真的按预期工作。这个脚本就是补这一环。

## 用法

1) 先用调试模式打开 Chrome（脚本本身不会替你启动浏览器）：

     "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
       --remote-debugging-port=9222 --remote-allow-origins=* ^
       --user-data-dir="C:\temp\apc_profile"

2) 然后运行：

     python check_probe_live.py

脚本会自己打开内置的 `probe_demo.html`（一个「输入 + 点保存弹提示」的最小页面），
合成一次输入与一次点击，然后把收到的探测事件逐项打印出来，并把自动生成的校验
写到 `probe_live_report.md`。

输出里重点看三件事：
  - `probes=N`：至少 2（两次探测都到货）
  - `acted_value`：应回读到 `C001`
  - `feedback`：应抓到 `保存成功`

## 注意

本脚本只操作它自己打开的 `probe_demo.html`，不影响你其它标签页。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recorder  # noqa: E402

DEMO = "file:///" + os.path.join(HERE, "probe_demo.html").replace("\\", "/")
REPORT = os.path.join(HERE, "probe_live_report.md")


def show(*a):
    print(*a)


def main():
    show("=" * 74)
    show("真机探测自检（需要已用调试模式打开 Chrome）")
    show("=" * 74)

    rec = recorder.WebRecorder(port=9222)
    try:
        rec.connect()
    except Exception as e:
        show("")
        show("❌ 连不上调试端口 9222：%s %s" % (type(e).__name__, e))
        show("")
        show("请先用调试模式启动 Chrome（注意 --remote-allow-origins=* 不能少）：")
        show('  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
             "--remote-debugging-port=9222 --remote-allow-origins=* "
             '--user-data-dir="C:\\temp\\apc_profile"')
        show("")
        show("顺带一提：若你的系统配了 HTTP_PROXY，旧版本会在这里报 502 Bad Gateway，")
        show("v3.6.0 起已对回环地址绕过代理（见 core/local_http.py）。")
        return 1

    rec.start()
    time.sleep(0.5)
    rec.send_cmd("Page.navigate", {"url": DEMO})
    time.sleep(2.0)
    rec._activate_context(blocking=True)
    time.sleep(0.8)

    # 合成一次输入
    rec.send_cmd("Runtime.evaluate", {"expression": """
      (function(){
        var el = document.getElementById('cust');
        el.value = 'C001';
        el.dispatchEvent(new Event('change', {bubbles:true}));
        return el.value;
      })()"""})
    time.sleep(0.5)

    # 合成一次点击（页面里的按钮会显示提示）
    rec.send_cmd("Runtime.evaluate", {"expression": """
      (function(){
        var b = document.getElementById('save');
        var r = b.getBoundingClientRect();
        var opt = {bubbles:true, cancelable:true,
                   clientX:r.left+5, clientY:r.top+5, button:0};
        b.dispatchEvent(new MouseEvent('mousedown', opt));
        b.dispatchEvent(new MouseEvent('mouseup', opt));
        return true;
      })()"""})

    time.sleep(3.0)                      # 等两次探测（+0.7s / +1.8s）
    events = rec.stop()
    rec.close()

    kinds = {}
    for e in events:
        kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
    probes = [e for e in events if e.get("type") == "probe"]
    changes = [e for e in events if e.get("type") == "change"]
    clicks = [e for e in events if e.get("type") == "click"]

    show("")
    show("收到事件：%s" % json.dumps(kinds, ensure_ascii=False))
    show("  change=%d  click=%d  probe=%d" % (len(changes), len(clicks), len(probes)))

    checks = []
    checks.append(("动作事件带动作前快照",
                   bool(changes) and isinstance(changes[0].get("pre"), dict)))
    checks.append(("探测事件到货", len(probes) >= 1))
    checks.append(("两次探测都到货（700/1800）",
                   sorted(set(p.get("delay") for p in probes)) == [700, 1800]))
    checks.append(("探测带 post 快照",
                   bool(probes) and isinstance(probes[0].get("post"), dict)))
    vals = [p.get("acted_value") for p in probes if p.get("acted_value") is not None]
    checks.append(("回读到录入值 C001", "C001" in vals))
    fb = []
    for p in probes:
        for item in ((p.get("post") or {}).get("feedback") or []):
            fb.append(item.get("text"))
    checks.append(("抓到反馈文案「保存成功」", "保存成功" in fb))

    show("")
    for name, okk in checks:
        show("  %s %s" % ("OK  " if okk else "FAIL", name))
    show("")
    show("  回读到的值：%s" % vals[:6])
    show("  抓到的反馈：%s" % fb[:6])

    # 把这条事件流喂给推断器，看现场能自动产出什么
    tf = recorder.events_to_taskflow(events)
    try:
        from core.verify_advisor import VerifyAdvisor
        adv = VerifyAdvisor(mode="auto")
        adv.attach(tf, events)
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write(adv.render_report("（真机自检，未写 task_flow.json）") + "\n")
        show("")
        show("自动生成的校验：")
        got = 0
        for st in tf:
            if st.get("verify"):
                got += 1
                show("  %s %s -> %s" % (st.get("func"), st.get("args"), st.get("verify")))
        if not got:
            show("  （没有写入。真机自检页只有 2 个动作，属正常；详见报告）")
        show("  报告：%s" % REPORT)
    except Exception as e:
        show("  推断器调用失败：%s %s" % (type(e).__name__, e))

    good = all(okk for _, okk in checks)
    show("")
    show("结论：%s" % ("✅ 真机探测正常" if good else "❌ 有项目未通过，见上面 FAIL"))
    return 0 if good else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
