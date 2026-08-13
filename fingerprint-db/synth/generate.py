"""
指纹合成器 — 从 GPU 能力表生成完整、内部一致的 device profile。

用法:
    python synth/generate.py [--count 60] [--out ../database]
    python synth/generate.py --gpu rtx4060 --variants 3   # 单型号多变体

一致性保证:
  - renderer/WebGL 参数/扩展 取自真实基线 (GPU 能力为公开硬件规格)
  - hw/mem 在型号真实搭配范围 (老卡不配 32 核, 新卡不配 4 核)
  - UA 平台版本与 GPU 时代匹配 (GT 1030 不配 Chrome 151)
  - screen 尺寸真实范围; audio 常见 44100/48000
  - speech voices 用 Windows SAPI 标准表
"""
import argparse
import json
import random
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = json.loads((ROOT / "synth" / "gpu_table.json").read_text(encoding="utf-8"))
DB = ROOT / "database"

# Windows SAPI 常见语音 (真实 Windows 系统枚举结果参考)
WINDOWS_VOICES = [
    {"name": "Microsoft Huihui - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Yaoyao - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Kangkang - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Zira - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Microsoft David - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Microsoft Mark - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
]

# GPU 世代 → Chrome 版本范围 (老 GPU 配老浏览器, 避免时间线矛盾)
GENERATION_CHROME = {
    "gt": (109, 120), "gtx10": (95, 120), "gtx16": (95, 125),
    "rtx20": (80, 120), "rtx30": (95, 130), "rtx40": (112, 135),
    "rx4": (70, 120), "rx5": (80, 120), "rx6": (100, 130),
    "rx7": (110, 135), "vega": (80, 120), "hd5": (80, 110),
    "hd6": (80, 115), "uhd": (95, 130), "iris": (100, 130), "arc": (110, 135),
}

SCREENS = [
    (1920, 1080, 1.0), (1920, 1080, 1.25), (1920, 1080, 1.5),
    (2560, 1440, 1.0), (2560, 1440, 1.25), (2560, 1440, 1.5),
    (1366, 768, 1.0), (1536, 864, 1.0), (1600, 900, 1.0),
    (3840, 2160, 1.5), (2880, 1800, 1.0),
]


def gen_screen():
    w, h, dpr = random.choice(SCREENS)
    return {
        "width": w, "height": h,
        "avail_width": w, "avail_height": h - 40,
        "color_depth": 24, "pixel_depth": 24,
        "device_pixel_ratio": dpr,
        "color_gamut": "srgb", "dynamic_range_high": False,
        "avail_left": 0, "avail_top": 0,
    }


def gen_hw_mem(gpu: dict, rng: random.Random):
    """hw/mem 在型号真实搭配范围浮动, 保持内部一致。"""
    base_hw = gpu.get("hw_range") or 8
    base_mem = gpu.get("mem_range") or 8
    pool = [4, 6, 8, 10, 12, 16, 20, 24, 28, 32]
    if base_hw <= 4:
        hw = 4
    elif base_hw <= 8:
        hw = rng.choice([4, 6, 8])
    elif base_hw <= 16:
        hw = rng.choice([8, 12, 16])
    else:
        hw = rng.choice([16, 20, 24, 28, 32])
    mem = base_mem
    if hw >= 16 and mem < 16:
        mem = rng.choice([16, 32])
    if hw <= 8 and mem > 16:
        mem = rng.choice([8, 16])
    return hw, mem


def gpu_generation(name: str) -> str:
    if name.startswith("gtx16"): return "gtx16"
    if name.startswith("gtx10"): return "gtx10"
    if name.startswith("rtx40"): return "rtx40"
    if name.startswith("rtx30"): return "rtx30"
    if name.startswith("rtx20") or name.startswith("rtx2"): return "rtx20"
    if name.startswith("gt"): return "gt"
    if name.startswith("rx7"): return "rx7"
    if name.startswith("rx6"): return "rx6"
    if name.startswith("rx5"): return "rx5"
    if name.startswith("rx4"): return "rx4"
    if name.startswith("vega"): return "vega"
    if name.startswith("uhd"): return "uhd"
    if name.startswith("iris"): return "iris"
    if name.startswith("arc"): return "arc"
    if name.startswith("hd"): return "hd6" if name[2:3] >= "6" else "hd5"
    return "gtx16"


def gen_ua(rng: random.Random, gpu: dict, name: str) -> tuple[str, int, str, str]:
    """UA + 品牌版本: 按 GPU 世代选 Chrome 版本, 与基线品牌保持一致。"""
    gen = gpu_generation(name)
    lo, hi = GENERATION_CHROME.get(gen, (100, 130))
    major = rng.randint(lo, hi)
    patch = rng.randint(0, 999)
    build = rng.randint(7000, 7999) if major >= 110 else rng.randint(4000, 6999)
    ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36")
    return ua, major, f"{major}.0.{build}.{patch}", f"{major}.0.0.0"


def gen_profile(gpu_name: str, rng: random.Random, variant: int) -> dict:
    gpu = TABLE[gpu_name]
    hw, mem = gen_hw_mem(gpu, rng)
    ua, major, full_ver, grease_full = gen_ua(rng, gpu, gpu_name)
    screen = gen_screen()
    renderer = gpu["renderer"]
    m = re.search(r"0x[0-9A-Fa-f]{4,8}", renderer)
    device_id = m.group(0) if m else ""
    gpu_family = renderer.split(",")[0].strip()
    arch_map = {"NVIDIA": {"rtx40": "ada", "rtx30": "ampere", "rtx20": "turing",
                           "gtx16": "turing", "gtx10": "pascal", "gt": "kepler"},
                "AMD": {"rx7": "rdna3", "rx6": "rdna2", "rx5": "rdna", "rx4": "polaris", "vega": "vega"},
                "Intel": {"arc": "alchemist", "iris": "xe", "uhd": "gen9", "hd": "gen8"}}
    vendor = gpu.get("vendor", "").split(" ")[0]
    arch = arch_map.get(vendor, {}).get(gpu_generation(gpu_name), "unknown")
    b64 = gpu.get("brand_full_version") or f"{major}.0.0.0"

    return {
        "name": f"{gpu_name}-v{variant}",
        "notes": renderer[:120],
        "timezone": "Asia/Shanghai",
        "icu_locale": "zh-CN",
        "navigator": {
            "language": "zh-CN",
            "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "languages": ["zh-CN", "zh", "en-US", "en"],
            "user_agent": ua,
            "platform": "Windows", "platform_value": "Win32",
            "platform_version": "19.0.0",
            "hardware_concurrency": hw, "device_memory": mem,
            "vendor": "Google Inc.", "max_touch_points": 0,
        },
        "client_hints": {
            "brand": "Google Chrome", "brand_version": str(major),
            "platform_version": "19.0.0", "architecture": "x86",
            "bitness": "64", "mobile": False,
            "grease_brand": "Not)A;Brand", "grease_version": "24",
            "chrome_build": int(gpu.get("brand_full_version", "").split(".")[1]) if "." in (gpu.get("brand_full_version") or "") else 7827,
            "chrome_patch": int(gpu.get("brand_full_version", "").split(".")[2]) if len((gpu.get("brand_full_version") or "").split(".")) > 2 else 103,
            "brand_full_version": full_ver,
            "grease_full_version": grease_full,
        },
        "screen": screen,
        "window": {
            "outer_width": screen["width"], "outer_height": screen["height"] - 1,
            "inner_width": screen["width"], "inner_height": screen["height"] - 136,
        },
        "webgl": {
            "vendor": f"Google Inc. ({vendor})",
            "renderer": renderer,
            "vendor_masked": "WebKit",
            "renderer_masked": "WebKit WebGL",
            "max_texture_size": gpu.get("max_texture_size") or 16384,
            "max_vertex_attribs": gpu.get("max_vertex_attribs") or 16,
            "extensions": gpu.get("extensions", []),
            "params": gpu.get("webgl_params", {}),
            "shader_precision": gpu.get("shader_precision", {}),
        },
        "webgl2": gpu.get("webgl2", {}),
        "webgpu": {
            "vendor": vendor.lower(),
            "architecture": arch,
            "device": device_id,
            "description": gpu_family,
            "limits": _webgpu_limits(arch),
        },
        "audio": {
            "sample_rate": 48000 if rng.random() < 0.7 else 44100,
            "channel_count": 2,
        },
        "connection": {"effective_type": "4g", "downlink_mbps": 10.0,
                       "rtt_msec": 75, "save_data": False},
        "storage_estimate": {"quota_gb": 10},
        "webauthn": {"uvpa": True},
        "memory": {"heap_size_limit": 4294967296},
        "battery": {"charging": True, "level": 1.0, "charging_time": 0,
                    "discharging_time": None},
        "media_devices": {"audio_input_count": 1, "audio_output_count": 1,
                          "video_input_count": 0},
        "speech": {"voices": WINDOWS_VOICES},
        "noise": {
            "canvas": {"enabled": True, "seed": rng.randint(1, 2 ** 31)},
            "webgl": {"enabled": False, "seed": 0, "intensity": 0},
            "audio": {"enabled": False, "seed": 0},
            "client_rects": {"enabled": False, "seed": 0, "max_offset": 0},
            "sensors": {"enabled": False, "seed": 0},
            "fonts": {"enabled": False, "seed": 0},
        },
        "tls": {
            "cipher_suites": [4865, 4866, 4867, 49195, 49199, 49196, 49200, 52393, 52392, 49171, 49172, 156, 157, 47, 53],
            "signature_algorithms": [1027, 2052, 1025, 1283, 2053, 1281, 2054, 1537],
            "shuffle_extensions": True,
        },
    }


def _webgpu_limits(arch: str) -> dict:
    """按 GPU 架构查 WebGPU limits (公开能力表)。"""
    base = {
        "maxTextureDimension1D": 16384, "maxTextureDimension2D": 16384,
        "maxTextureDimension3D": 2048, "maxTextureArrayLayers": 2048,
        "maxBindGroups": 4, "maxBindGroupsPlusVertexBuffers": 24,
        "maxBindingsPerBindGroup": 1000, "maxDynamicUniformBuffersPerPipelineLayout": 8,
        "maxDynamicStorageBuffersPerPipelineLayout": 4,
        "maxSampledTexturesPerShaderStage": 16, "maxSamplersPerShaderStage": 16,
        "maxStorageBuffersPerShaderStage": 8, "maxStorageTexturesPerShaderStage": 4,
        "maxUniformBuffersPerShaderStage": 12, "maxUniformBufferBindingSize": 65536,
        "maxStorageBufferBindingSize": 2147483647, "minUniformBufferOffsetAlignment": 256,
        "minStorageBufferOffsetAlignment": 256, "maxVertexBuffers": 8,
        "maxBufferSize": 268435456, "maxVertexAttributes": 16,
        "maxVertexBufferArrayStride": 2048, "maxInterStageShaderVariables": 15,
        "maxColorAttachments": 8, "maxColorAttachmentBytesPerSample": 32,
        "maxComputeWorkgroupStorageSize": 16384, "maxComputeInvocationsPerWorkgroup": 256,
        "maxComputeWorkgroupSizeX": 256, "maxComputeWorkgroupSizeY": 256,
        "maxComputeWorkgroupSizeZ": 64, "maxComputeWorkgroupsPerDimension": 65535,
    }
    # 现代架构 (ada/rdna3/rdna2/alchemist) 更高规格
    if arch in ("ada", "rdna3", "rdna2", "alchemist"):
        base.update({
            "maxSampledTexturesPerShaderStage": 16, "maxStorageBuffersPerShaderStage": 8,
            "maxUniformBuffersPerShaderStage": 12, "maxBufferSize": 2684354560,
            "maxVertexAttributes": 30, "maxInterStageShaderVariables": 30,
        })
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=60, help="生成 profile 总数")
    ap.add_argument("--gpu", default=None, help="只生成指定型号")
    ap.add_argument("--variants", type=int, default=3, help="每型号变体数")
    args = ap.parse_args()

    DB.mkdir(parents=True, exist_ok=True)
    rng = random.Random(datetime.now().timestamp())

    targets = [args.gpu] if args.gpu else list(TABLE.keys())
    count = 0
    for gpu_name in targets:
        for v in range(1, args.variants + 1):
            if args.count and count >= args.count:
                break
            prof = gen_profile(gpu_name, rng, v)
            path = DB / f"{prof['name']}.json"
            path.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1
        if args.count and count >= args.count:
            break
    print(f"[synth] 生成 {count} 套 profile → {DB}")


if __name__ == "__main__":
    main()
