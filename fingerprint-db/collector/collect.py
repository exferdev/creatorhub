"""
真机指纹采集器 — 在真实系统 Chrome 上运行 probe.js, 输出 ShardX 兼容 JSON。

用法:
    python collector/collect.py --name win-rtx4060   # 命名采集 (默认取 GPU 型号)
    python collector/collect.py --name my-pc --repeat 3

输出: fingerprint-db/database/<name>.json
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "database"
PROBE = (ROOT / "collector" / "probe.js").read_text(encoding="utf-8")


async def collect(name: str, repeat: int = 1) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", headless=False,
            ignore_default_args=["--no-sandbox"],
        )
        page = await browser.new_page()
        try:
            await page.goto("https://example.com", timeout=15000)
        except Exception:
            pass
        data = None
        for i in range(repeat):
            data = await page.evaluate(PROBE)
            if data.get("webgl", {}).get("renderer"):
                break
            await page.wait_for_timeout(1500)
        await browser.close()

    if not data or not data.get("webgl", {}).get("renderer"):
        raise RuntimeError("WebGL renderer 采集失败")

    if name == "auto":
        renderer = data["webgl"]["renderer"]
        m = re.search(r"([A-Za-z0-9 -]+?)\s*\(", renderer.split("ANGLE (")[-1])
        name = m.group(1).strip().replace(" ", "-").lower() if m else "device"
    data["name"] = name
    data["notes"] = data["webgl"]["renderer"][:120]
    return data


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="auto")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    data = await collect(args.name, args.repeat)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    path = DB_DIR / f"{data['name']}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[collect] 保存 → {path}")
    print(f"  GPU: {data['webgl']['renderer'][:70]}")
    print(f"  UA: {data['navigator']['user_agent'][:60]}")
    print(f"  HW: {data['navigator']['hardware_concurrency']} cores / {data['navigator']['device_memory']} GB")
    print(f"  扩展: {len(data['webgl'].get('extensions', []))} | 参数: {len(data['webgl'].get('params', {}))}")
    print(f"  webgl2: {bool(data.get('webgl2'))} | webgpu limits: {len((data.get('webgpu') or {}).get('limits', {}))}")
    print(f"  audio: {data.get('audio', {}).get('sample_rate', 'ERR')}Hz | speech voices: {len((data.get('speech') or {}).get('voices', []))}")


if __name__ == "__main__":
    asyncio.run(main())
