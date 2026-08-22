# -*- coding: utf-8 -*-
"""真实站点实测：百度搜索（对话驱动 + 预置元素库 + 真实浏览器）"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_launcher import launch
from cdp_engine import CdpBrowserCtrl
import recorder as rec_mod
import core.element_repo as er

PORT = 9355
URL = "https://www.baidu.com"

def main():
    print("[1] 启动真实 Chrome ...")
    launch(port=PORT, open_url=URL, headless=True, reuse=True)
    ctrl = CdpBrowserCtrl(PORT)
    ctrl.connect(url_filter=URL)
    time.sleep(2)

    print("[2] 验证预置元素命中 ...")
    repo = er.ElementRepository.load_preset(os.path.join(os.path.dirname(os.path.abspath(__file__)), "preset_elements.json"))
    # 检查 baidu 预置元素是否存在
    found_inputs = [e.eid for e in repo.elements.values() if e.eid in ("preset_baidu_search_input","preset_baidu_search_btn")]
    print("    预置元素:", found_inputs)

    print("[3] 真实输入 + 点击搜索 ...")
    ctrl.input_text("#wd", "AutoPilot Composer 自动化")
    time.sleep(0.5)
    ctrl.click_elem("#su")
    time.sleep(3)

    print("[4] 验证页面真实跳转（搜索结果）...")
    cur = ctrl.get_current_url()
    print("    当前 URL:", cur)
    html = ctrl.get_html() or ""
    has_result = ("baidu.com/s" in cur) or ("result" in html.lower()) or ("搜索结果" in html)
    print("    命中搜索结果页:", has_result)

    print("[5] 交互式录制（非 GUI 自动确认）...")
    rec = rec_mod.InteractiveRecorder(port=PORT, confirm=False)
    cur_url = ctrl.get_current_url()
    wd = rec.prepare(first_url=cur_url)
    rec.run_loop()  # 立即结束（无事件），但已初始化落盘
    rec.stop()
    time.sleep(0.5)
    print("    录制目录:", wd)

    print("\n=== 真实站点实测结论 ===")
    print("URL 跳转:", "PASS" if has_result else "FAIL")
    print("预置命中:", "PASS" if len(found_inputs)==2 else "FAIL")
    return has_result and len(found_inputs)==2

if __name__ == "__main__":
    try:
        ok = main()
        print("\nRESULT:", "ALL_PASS" if ok else "PARTIAL")
    except Exception as e:
        import traceback; traceback.print_exc()
        print("RESULT: ERROR")
