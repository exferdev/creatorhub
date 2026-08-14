"""PoC 第二轮: 标准指纹图案 canvas + audio 频谱 + deviceMemory"""
import asyncio, json, sys
from playwright.async_api import async_playwright

EXE = ("E:/creatorhub/data/fingerprint-chromium/"
       "ungoogled-chromium_148.0.7778.215-1.1_windows_x64/chrome.exe")

async def probe(p, seed):
    b = await p.chromium.launch(
        executable_path=EXE, headless=False,
        args=[f"--fingerprint={seed}", "--no-first-run"],
        ignore_default_args=["--no-sandbox", "--enable-automation", "--disable-infobars"],
    )
    pg = await b.new_page()
    r = await pg.evaluate("""() => {
        // 标准指纹画布 (CreepJS 风格)
        const c = document.createElement('canvas');
        c.width = 300; c.height = 150;
        const ctx = c.getContext('2d');
        ctx.textBaseline = 'top'; ctx.font = '14px Arial';
        ctx.fillStyle = '#f60'; ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = '#069'; ctx.fillText('Cwm fjordbank glyphs vext quiz, \\ud83d\\ude03', 2, 15);
        ctx.fillStyle = 'rgba(102, 204, 0, 0.7)';
        ctx.fillText('Cwm fjordbank glyphs vext quiz, \\ud83d\\ude03', 4, 17);
        const b64 = c.toDataURL();
        const data = [...c.getContext('2d').getImageData(0, 0, 300, 150).data];
        let ch = 0;
        for (let i = 0; i < data.length; i += 997) ch = (ch * 31 + data[i]) | 0;
        // audio 频谱 (getChannelData 前若干样本哈希)
        let audioHash = 'NO';
        try {
            const actx = new AudioContext();
            const len = 44100;
            const buf = actx.createBuffer(1, len, 44100);
            const d = buf.getChannelData(0);
            let ah = 0;
            for (let i = 0; i < len; i += 500) ah = (ah * 33 + Math.round(d[i] * 1e9)) | 0;
            audioHash = (ah >>> 0).toString(16);
            actx.close();
        } catch (e) { audioHash = 'ERR:' + e.message.slice(0, 20); }
        return {
            toDataURL_head: b64.slice(0, 30),
            canvasHash: (ch >>> 0).toString(16),
            audioHash,
            deviceMemory: typeof navigator.deviceMemory !== 'undefined' ? navigator.deviceMemory : 'UNDEF',
            hardwareConcurrency: navigator.hardwareConcurrency,
            ua: navigator.userAgent.slice(-30),
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
            print(f"seed={seed}: canvas={r['canvasHash']} audio={r['audioHash']} mem={r['deviceMemory']} hw={r['hardwareConcurrency']}")
        print(f"\ncanvas 唯一: {len(set(v['canvasHash'] for v in results.values()))}/4")
        print(f"audio 唯一: {len(set(v['audioHash'] for v in results.values()))}/4")
        print(f"deviceMemory: {[v['deviceMemory'] for v in results.values()]}")

asyncio.run(main())
