"""浏览器端到端验收：厂商一键切换 UI。

验 6 件事（都是用户能看见的）：
1. 设置页加载后厂商下拉框真的填上了 7 家，且预选中当前厂商
2. 选中某家后，模型输入框被填上该家第一个模型，预览行显示将要写入的 base_url
3. 点「切换并保存」成功，状态变成绿色「已切换」
4. 下面那三行（Base URL / 模型 / API Key）**跟着一起变**——不能出现
   「上面说切到 Kimi、下面还写着 deepseek」这种自相矛盾的界面
5. 切回 DeepSeek 成功
6. 全程控制台无错误、页面上没有 undefined

依赖系统 Chrome（channel="chrome"）。
"""

from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8020")
TOKEN = os.environ.get("E2E_TOKEN", "browser-test-admin-2026")
URL = BASE + "/ui/settings.html"


def main() -> int:
    fails: list[str] = []
    console_errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
        if not ok:
            fails.append(name + (": " + detail if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # ---- 登录
        page.goto(BASE + "/ui/admin-login.html", wait_until="domcontentloaded")
        page.fill("#token" if page.locator("#token").count() else "input[type=password]", TOKEN)
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"settings\.html"), timeout=15000)
        print("登录成功，进入设置页")

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => { const s = document.getElementById('provSel');"
            " return s && s.options.length > 1 && s.options[0].value !== ''; }",
            timeout=20000,
        )

        # ---- 1 下拉框
        opts = page.eval_on_selector_all("#provSel option", "els => els.map(e => e.value)")
        check("下拉框列出全部厂商", len(opts) >= 7, f"共 {len(opts)} 项: {opts}")
        cur = page.input_value("#provSel")
        check("预选中当前厂商 deepseek", cur == "deepseek", f"实际 {cur}")

        # ---- 2 选中 moonshot
        page.select_option("#provSel", "moonshot")
        page.wait_for_timeout(300)
        model_val = page.input_value("#provModel")
        check("选 Kimi 后模型框填 moonshot-v1-8k", model_val == "moonshot-v1-8k", model_val)
        preview = page.inner_text("#provPreview")
        check("预览显示将写入的 base_url", "api.moonshot.cn" in preview, preview)
        body_visible = page.eval_on_selector("#provBody", "el => !el.hidden")
        check("切换表单已展开", body_visible)

        # ---- 3 保存
        page.click("#btnProvSave")
        page.wait_for_function(
            "() => { const s = document.getElementById('provStat');"
            " return s && /已切换/.test(s.textContent); }",
            timeout=20000,
        )
        stat = page.inner_text("#provStat")
        ok_cls = page.eval_on_selector("#provStat", "el => el.className")
        check("切换成功提示", "已切换" in stat, stat)
        check("状态是绿色 ok 样式", "ok" in ok_cls.split(), ok_cls)

        # ---- 4 下面三行跟着变
        page.wait_for_timeout(500)
        base_input = page.input_value('[data-key="LLM_BASE_URL"]')
        model_input = page.input_value('[data-key="LLM_MODEL"]')
        check("Base URL 那一行同步为 moonshot", "moonshot.cn" in base_input, base_input)
        check("模型那一行同步为 moonshot-v1-8k", model_input == "moonshot-v1-8k", model_input)

        # ---- 5 切回 deepseek
        page.select_option("#provSel", "deepseek")
        page.wait_for_timeout(200)
        page.select_option("#provSel", "deepseek")
        page.click("#btnProvSave")
        page.wait_for_function(
            "() => { const s = document.getElementById('provStat');"
            " return s && /已切换/.test(s.textContent); }",
            timeout=20000,
        )
        page.wait_for_timeout(400)
        base_input = page.input_value('[data-key="LLM_BASE_URL"]')
        check("切回后 Base URL 是 deepseek", "api.deepseek.com" in base_input, base_input)

        # ---- 6 页面整洁
        body_text = page.inner_text("body")
        check("页面无 undefined", "undefined" not in body_text)
        check("控制台无错误", not console_errors, "; ".join(console_errors[:3]))

        page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "settings-provider.png"),
                        full_page=False)
        browser.close()

    print()
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())