"""BrowserScan 实测: 合成 profile (rtx4060-v1) 经 ShardX 引擎的真实检测分数"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shardx import ShardX, Profile

DB = Path(__file__).resolve().parent.parent / "database"

async def main():
    sdk = ShardX()
    prof_file = sorted(DB.glob("rtx4060-v*.json"))[0]
    prof = Profile.from_file(str(prof_file))
    print(f"profile: {prof.config['name']} | GPU: {prof.config['webgl']['renderer'][:55]}")

    async with sdk.session(prof, headless=False) as browser:
        ctx = browser.contexts[0]
        pg = await ctx.new_page()
        try:
            await pg.goto("https://browserscan.net/zh", timeout=60000)
            await pg.wait_for_timeout(20000)
            r = await pg.evaluate("""() => {
                const text = document.body.innerText;
                const grab = (kw) => {
                    const i = text.indexOf(kw);
                    if (i < 0) return 'N/A';
                    return text.slice(i, i + 50).replace(/\\n/g, ' ');
                };
                return {
                    incognito: grab('隐身'),
                    webgl: grab('WebGL'),
                    audio: grab('Audio') || grab('音频'),
                    os: grab('操作系统') || grab('系统'),
                    browser: grab('浏览器'),
                };
            }""")
            print("\nBrowserScan:", r)
        except Exception as e:
            print("ERROR:", str(e)[:150])
        await pg.close()

asyncio.run(main())
