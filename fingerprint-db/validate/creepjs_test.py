"""CreepJS 结果抓取 v2: 全文扫描关键标记"""
import asyncio, sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shardx import ShardX, Profile

DB = Path(__file__).resolve().parent.parent / "database"

async def main():
    sdk = ShardX()
    prof = Profile.from_file(str(sorted(DB.glob("rtx4060-v*.json"))[0]))
    async with sdk.session(prof, headless=False) as browser:
        ctx = browser.contexts[0]
        pg = await ctx.new_page()
        await pg.goto("https://abrahamjuliot.github.io/creepjs/", timeout=90000)
        await pg.wait_for_timeout(40000)
        text = await pg.evaluate("document.body.innerText")
        with open(Path(__file__).resolve().parent / "_creepjs_dump.txt", "w", encoding="utf-8") as f:
            f.write(text)
        # 扫描关键标记
        patterns = {
            "lies": r"lies?[:\s]|lied|bold-fail|suspicious",
            "trust": r"trust[^a-z]|trust\s+score|Trust",
            "bot": r"bot[^a-z]|Bot",
            "webgl_lowentropy": r"lower.?entropy|low entropy|webgl",
            "audio_trap": r"trap|sample",
            "canvas": r"canvas",
        }
        print("--- 标记扫描 ---")
        for name, pat in patterns.items():
            hits = re.findall(pat, text, re.IGNORECASE)
            print(f"{name}: {len(hits)} 处命中")
        # 输出含 webgl/audio/canvas 的上下文行
        print("--- 相关行 ---")
        for line in text.split("\n"):
            l = line.strip()
            if not l or len(l) > 100:
                continue
            if re.search(r"webgl|audio|canvas|lies|trust|bot|entropy", l, re.IGNORECASE):
                print(f"  {l[:90]}")
        await pg.close()

asyncio.run(main())
