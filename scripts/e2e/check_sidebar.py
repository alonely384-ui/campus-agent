"""浏览器实测侧栏历史对话点击 + 右侧拖拽行为。

要验证的事：
1. 造两条会话写进 localStorage，刷新 index.html 后侧栏能列出它们；
2. 点击历史条目能真的打开那条会话（用户反馈"点击没反应显示不了"）；
3. 拖动右侧拉手改变的是侧栏宽度，聊天区跟着收窄且紧贴侧栏
   （用户反馈"应该是聊天内容对话本身的拉升，和harness一样才对"）；
4. 鼠标离开时拉手不显示，悬停时才出现（用户反馈"鼠标没悬停时，不显示右侧拉动长条"）；
5. 页脚不出现 undefined（安全整改后 /health 对匿名访客不再返回 model 字段）。

用法（需要先起一个本地服务）：

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8014
    .venv\\Scripts\\python.exe scripts/e2e/check_sidebar.py

环境变量 E2E_BASE 可以覆盖默认地址。依赖系统 Chrome（`channel="chrome"`），
因为本机没有下载 Playwright 自带的浏览器。
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8014/ui/index.html")
SESSIONS_KEY = "campus-agent-sessions-v1"

SEED = {
    "sessions": [
        {
            "id": "sess-alpha",
            "title": "注意力机制的第一作者是谁",
            "createdAt": 1761900000000,
            "updatedAt": 1761900500000,
            "messages": [
                {"role": "user", "content": "注意力机制的第一作者是谁"},
                {"role": "assistant", "content": "Ashish Vaswani。"},
            ],
        },
        {
            "id": "sess-beta",
            "title": "对比 RAG 与长上下文方案",
            "createdAt": 1761901000000,
            "updatedAt": 1761901500000,
            "messages": [
                {"role": "user", "content": "对比 RAG 与长上下文方案"},
                {"role": "assistant", "content": "两者各有取舍。"},
            ],
        },
    ]
}


async def main() -> int:
    failures: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 先落地一次，才能对同源写 localStorage
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.evaluate(
            "([k, v]) => localStorage.setItem(k, JSON.stringify(v))",
            [SESSIONS_KEY, SEED],
        )
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.wait_for_timeout(600)

        # --- 1. 侧栏列出两条历史 ---
        items = await page.locator("#sbHistory .sb-hist").count()
        print(f"[1] 侧栏历史条数 = {items}（期望 2）")
        if items != 2:
            failures.append(f"历史条数 {items} != 2")

        # --- 2. 点击历史能打开那条会话 ---
        # 侧栏按 updatedAt 倒序排，sess-beta（更新）在第 0 条，sess-alpha 在第 1 条。
        # 这里点第 0 条，验证的是「最近的那条能一键打开」——这正是用户最常走的路径。
        await page.locator("#sbHistory .sb-hist__open").nth(0).click()
        await page.wait_for_timeout(900)
        url = page.url
        body_text = await page.inner_text("body")
        has_msg = ("对比 RAG 与长上下文方案" in body_text)
        has_ans = ("两者各有取舍" in body_text)
        print(f"[2] 点击后 URL = {url}")
        print(f"[2] 会话标题出现在页面 = {has_msg}，回答出现在页面 = {has_ans}")
        if not (has_msg and has_ans):
            failures.append("点击历史后会话内容没有显示")

        # --- 3. 拖拽拉手只改侧栏宽度，聊天区不被推走 ---
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.wait_for_timeout(500)
        resizer = page.locator("#sbResizer")
        box = await resizer.bounding_box()
        sidebar = page.locator(".sidebar")
        before_side = await sidebar.bounding_box()
        chat = page.locator(".app__main")
        before_chat = await chat.bounding_box() if await chat.count() else None
        print(f"[3] 拖前 侧栏宽 = {before_side['width']:.0f}, 拉手 x = {box['x']:.0f}")

        if box:
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
            await page.mouse.down()
            await page.mouse.move(box["x"] + 120, box["y"] + 200, steps=12)
            await page.mouse.up()
            await page.wait_for_timeout(400)
        after_side = await sidebar.bounding_box()
        after_chat = await chat.bounding_box() if before_chat else None
        print(f"[3] 拖后 侧栏宽 = {after_side['width']:.0f}（应变大）")
        if before_chat and after_chat:
            print(f"[3] 聊天区 x = {before_chat['x']:.0f} -> {after_chat['x']:.0f}，"
                  f"宽 = {before_chat['width']:.0f} -> {after_chat['width']:.0f}")
            # 判据：聊天区必须「宽度收窄」且「紧贴侧栏右缘」。
            # 布局是 flex（.sidebar 与 .app__main 并排），侧栏变宽时聊天区 x 后移是必然的；
            # 用户 m15589 说的「应该是聊天内容对话本身的拉升」，指的是对话正文跟着收窄，
            # 而不是整个聊天区保留原宽、只把头往右挪。所以要验宽度与贴合度，不验 x。
            if after_chat["width"] >= before_chat["width"] - 2:
                failures.append("聊天区没变窄，拖拽没作用到对话内容")
            gap = after_chat["x"] - after_side["width"]
            print(f"[3] 聊天区左缘 - 侧栏右缘 = {gap:.0f}px（应 ≈ 0，说明紧贴）")
            if abs(gap) > 2:
                failures.append(f"聊天区没有紧贴侧栏（间隙 {gap:.0f}px）")
        if after_side["width"] <= before_side["width"] + 2:
            failures.append("侧栏没变宽")

        # --- 4. 鼠标离开时不显示拉手 ---
        await page.mouse.move(700, 60)
        await page.wait_for_timeout(500)
        opacity = await resizer.evaluate(
            "el => { const s = getComputedStyle(el); return {op: s.opacity, w: s.width, bg: s.backgroundColor}; }"
        )
        print(f"[4] 鼠标离开时拉手样式 = {opacity}")
        if float(opacity["op"]) > 0.05 and opacity["bg"] not in ("rgba(0, 0, 0, 0)",):
            failures.append(f"鼠标离开时拉手仍可见 opacity={opacity['op']} bg={opacity['bg']}")

        print(f"[console] 错误数 = {len(errors)}")
        for e in errors[:5]:
            print("   ", e)
        if errors:
            failures.append(f"控制台有 {len(errors)} 条错误")

        # --- 5. 页脚不能出现 undefined（/health 匿名不返回 model 字段）---
        footer = await page.inner_text("#sbStatus, .status, body")
        has_undef = "undefined" in footer
        print(f"[5] 页脚出现 undefined = {has_undef}")
        if has_undef:
            failures.append("页脚出现 undefined（/health 的 model 字段匿名不给）")

        await browser.close()

    print("=" * 50)
    if failures:
        print("失败项：")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))