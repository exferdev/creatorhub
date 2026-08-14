"""PoC: fingerprint-chromium 引擎级指纹差异化验证"""
import asyncio, json, sys
from playwright.async_api import async_playwright

EXE = ("E:/creatorhub/data/fingerprint-chromium/"
       "ungoogled-chromium_148.0.7778.215-1.1_windows_x64/chrome.exe")

async def probe(p, seed):
    b = await p.chromium.launch(
        executable_path=EXE,
        headless=False,
        args=[f"--fingerprint={seed}", "--no-first-run"],
        ignore_default_args=["--no-sandbox", "--enable-automation", "--disable-infobars"],
    )
    pg = await b.new_page()
    r = await pg.evaluate("""() => {
        const isNative = (f) => { try { return f.toString().includes('[native code]'); } catch (e) { return 'ERR'; } };
        // canvas hash
        const c = document.createElement('canvas');
        c.width = 256; c.height = 256;
        const ctx = c.getContext('2d');
        ctx.textBaseline = 'top'; ctx.font = '14px Arial';
        ctx.fillStyle = '#f60'; ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = '#069'; ctx.fillText('Cwm fjordbank glyphs vext quiz', 2, 15);
        const data = [...ctx.getImageData(0, 0, 256, 256).data];
        let ch = 0;
        for (let i = 0; i < data.length; i += 997) ch = (ch * 31 + data[i]) | 0;
        // WebGL renderer
        let gl = null;
        try { gl = document.createElement('canvas').getContext('webgl'); } catch (e) {}
        let renderer = 'NO';
        if (gl) {
            try {
                const ext = gl.getExtension('WEBGL_debug_renderer_info');
                renderer = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : String(gl.getParameter(37446));
            } catch (e) { renderer = 'ERR'; }
        }
        // audio hash
        let audioHash = 'NO_AUDIO';
        try {
            const actx = new (window.AudioContext || window.webkitAudioContext)();
            const o = actx.createOscillator(); o.frequency.value = 1000;
            const g = actx.createGain(); g.gain.value = 0;
            o.connect(g); g.connect(actx.destination);
            o.start();
            const dst = actx.createMediaStreamDestination();
            o.disconnect(); o.connect(dst);
            const chData = dst.stream.getAudioTracks()[0];
            audioHash = chData ? String(chData.id).length : 'TRACK';
            actx.close();
        } catch (e) {}
        return {
            seed_native: isNative(HTMLCanvasElement.prototype.toDataURL),
            getImageData_native: isNative(CanvasRenderingContext2D.prototype.getImageData),
            webdriver: navigator.webdriver,
            canvasHash: (ch >>> 0).toString(16),
            renderer: renderer.slice(0, 60),
            deviceMemory: navigator.deviceMemory,
            hardwareConcurrency: navigator.hardwareConcurrency,
        };
    }""")
    await b.close()
    return r

async def main():
    async with async_playwright() as p:
        results = {}
        for seed in ["11111111", "22222222", "33333333", "44444444"]:
            r = await probe(p, seed)
            results[seed] = r
            print(f"seed={seed}: canvas={r['canvasHash']} gpu={r['renderer'][:40]} mem={r['deviceMemory']} native={r['getImageData_native']} webdriver={r['webdriver']}")
        # 差异化统计
        hashes = [v['canvasHash'] for v in results.values()]
        renderers = [v['renderer'] for v in results.values()]
        print(f"\ncanvas 唯一指纹数: {len(set(hashes))}/{len(hashes)}")
        print(f"GPU 唯一renderer数: {len(set(renderers))}/{len(renderers)}")
        print(f"deviceMemory 分布: {[v['deviceMemory'] for v in results.values()]}")

asyncio.run(main())
