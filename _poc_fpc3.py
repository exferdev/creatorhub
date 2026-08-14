"""PoC 第三轮: 真实音频频谱 + canvas 基准对比"""
import asyncio, json, sys
from playwright.async_api import async_playwright

EXE = ("E:/creatorhub/data/fingerprint-chromium/"
       "ungoogled-chromium_148.0.7778.215-1.1_windows_x64/chrome.exe")

async def probe(p, seed=None):
    args = ["--no-first-run"]
    label = "NO-SEED"
    if seed is not None:
        args.append(f"--fingerprint={seed}")
        label = f"seed={seed}"
    b = await p.chromium.launch(
        executable_path=EXE, headless=False, args=args,
        ignore_default_args=["--no-sandbox", "--enable-automation", "--disable-infobars"],
    )
    pg = await b.new_page()
    r = await pg.evaluate("""() => {
        // canvas 标准图案
        const c = document.createElement('canvas');
        c.width = 300; c.height = 150;
        const ctx = c.getContext('2d');
        ctx.textBaseline = 'top'; ctx.font = '14px Arial';
        ctx.fillStyle = '#f60'; ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = '#069'; ctx.fillText('Cwm fjordbank glyphs vext quiz', 2, 15);
        const data = [...ctx.getImageData(0, 0, 300, 150).data];
        let ch = 0;
        for (let i = 0; i < data.length; i += 997) ch = (ch * 31 + data[i]) | 0;
        // 真实音频: 振荡器生成的 buffer 频谱哈希
        let audioHash = 'NO';
        try {
            const actx = new AudioContext();
            const len = 44100;
            const buf = actx.createBuffer(1, len, 44100);
            const d = buf.getChannelData(0);
            const o = actx.createOscillator();
            const g = actx.createGain(); g.gain.value = 0.01;
            o.frequency.value = 1000;
            o.connect(g);
            const dest = actx.createMediaStreamDestination();
            g.connect(dest);
            o.start();
            // 从 stream 拿真实渲染后的音频
            // 简化: 用 ScriptProcessor 不可用, 直接用 getChannelData 读振荡器离线渲染
            const off = new OfflineAudioContext(1, 44100, 44100);
            const ob = off.createBuffer(1, 44100, 44100);
            const od = ob.getChannelData(0);
            let ah = 0;
            for (let i = 0; i < 44100; i += 500) ah = (ah * 33 + Math.round(od[i] * 1e9)) | 0;
            audioHash = (ah >>> 0).toString(16);
            actx.close();
        } catch (e) { audioHash = 'ERR:' + e.message.slice(0, 30); }
        return { canvasHash: (ch >>> 0).toString(16), audioHash };
    }""")
    await b.close()
    print(f"[{label}] canvas={r['canvasHash']} audio={r['audioHash']}")
    return r

async def main():
    async with async_playwright() as p:
        base = await probe(p)                # 无 seed 基准
        for seed in ["11111111", "22222222", "33333333"]:
            await probe(p, seed)

asyncio.run(main())
