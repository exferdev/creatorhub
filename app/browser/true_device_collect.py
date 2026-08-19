"""真机指纹采集器: 在本机真实 Chrome 上跑 probe.js, 输出 ShardX 兼容 JSON。

集成自 fingerprint-db 的 collector (collect.py + probe.js)。creatorhub 首次启动时
自动采集一次并上传到 fingerprint-db (POST /api/v1/collect/raw), 扩充真机基线。

无头补丁: UA 去除 HeadlessChrome 标记 + 窗口固定主流尺寸(1920x1080),
采集产物即干净基线 (通过指纹库脏样本拒收)。
"""
from __future__ import annotations

import re
from pathlib import Path

PROBE_JS = (Path(__file__).resolve().parent / "fingerprint_probe.js").read_text(encoding="utf-8")


async def collect_true_device(headless: bool = True,
                              window_size: str = "1920x1080") -> dict:
    """在本机真实 Chrome 上采集真机指纹, 返回 ShardX 兼容的 profile dict。"""
    from patchright.async_api import async_playwright

    w, h = map(int, window_size.replace("x", ",").split(","))
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", headless=headless,
            ignore_default_args=["--no-sandbox"],
            args=[f"--window-size={w},{h}"],
        )
        page = await browser.new_page(viewport={"width": w, "height": h})
        try:
            await page.goto("https://example.com", timeout=15000)
        except Exception:
            pass
        ua = await page.evaluate("navigator.userAgent")
        ctx_kwargs = {"viewport": {"width": w, "height": h}}
        if headless and "HeadlessChrome" in ua:
            ctx_kwargs["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            await page.goto("https://example.com", timeout=15000)
        except Exception:
            pass
        data = None
        for _ in range(3):
            data = await page.evaluate(PROBE_JS)
            if data.get("webgl", {}).get("renderer"):
                break
            await page.wait_for_timeout(1500)
        await browser.close()

    if not data or not data.get("webgl", {}).get("renderer"):
        raise RuntimeError("WebGL renderer 采集失败")

    data["name"] = auto_name(data)
    data["notes"] = data["webgl"]["renderer"][:120]
    return data


def auto_name(data: dict) -> str:
    """从 WebGL renderer 提取 GPU 型号作为样本名 (小写, 空格转 -)。

    例: "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti SUPER ...)" → "nvidia-geforce-rtx-4070-ti-super"
    """
    renderer = data["webgl"]["renderer"]
    try:
        inner = renderer.split("ANGLE (")[-1]
        m = re.search(r"([A-Za-z0-9 -]+?)\s*\(", inner)
        name = m.group(1).strip().replace(" ", "-").lower() if m else "device"
    except Exception:
        name = "device"
    # 指纹库命名规范: 只允许 [a-z0-9-]
    return re.sub(r"[^a-z0-9-]", "-", name).strip("-") or "device"
