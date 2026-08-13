"""对接测试: 合成 profile 用 ShardX 引擎加载启动, 验证指纹生效"""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shardx import ShardX, Profile

DB = Path(__file__).resolve().parent.parent / "database"

async def main():
    sdk = ShardX()
    # 选一个合成 profile
    prof_file = sorted(DB.glob("rtx4060-v*.json"))[0]
    prof = Profile.from_file(str(prof_file))
    cfg = prof.config
    print(f"加载: {cfg['name']}")
    print(f"  GPU: {cfg['webgl']['renderer'][:60]}")
    print(f"  HW: {cfg['navigator']['hardware_concurrency']}核/{cfg['navigator']['device_memory']}GB")
    print(f"  UA: {cfg['navigator']['user_agent'][:55]}")

    async with sdk.session(prof, headless=True) as browser:
        ctx = browser.contexts[0]
        pg = await ctx.new_page()
        r = await pg.evaluate("""() => {
            let gl = null;
            try { gl = document.createElement('canvas').getContext('webgl'); } catch (e) {}
            let renderer = 'NO';
            if (gl) {
                const ext = gl.getExtension('WEBGL_debug_renderer_info');
                renderer = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : 'NO_EXT';
            }
            return {
                ua: navigator.userAgent.slice(0, 55),
                hw: navigator.hardwareConcurrency,
                mem: typeof navigator.deviceMemory !== 'undefined' ? navigator.deviceMemory : 'UNDEF',
                renderer: renderer.slice(0, 55),
                webdriver: navigator.webdriver,
            };
        }""")
        print("\n浏览器实测:")
        print(f"  UA: {r['ua']}")
        print(f"  HW: {r['hw']}核 / mem={r['mem']}GB")
        print(f"  GPU: {r['renderer']}")
        print(f"  webdriver: {r['webdriver']}")
        # 对比一致性
        ua_ok = r['ua'].startswith(cfg['navigator']['user_agent'][:30])
        hw_ok = r['hw'] == cfg['navigator']['hardware_concurrency']
        gpu_ok = r['renderer'] == cfg['webgl']['renderer'][:55]
        print(f"\nUA一致: {ua_ok} | HW一致: {hw_ok} | GPU一致: {gpu_ok}")
        await pg.close()

asyncio.run(main())
