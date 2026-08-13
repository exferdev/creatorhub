"""
腾讯云 SCF 复用 — profile 合成纯函数。

从 fingerprint-db/synth/generate.py 抽出, 无 CLI 依赖, 可被 SCF synth 直接 import。
generate_profiles(platform, table, variants, seed) → list[profile dict]
"""
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# 平台配置 (与 synth/generate.py 一致)
WINDOWS_VOICES = [
    {"name": "Microsoft Huihui - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Yaoyao - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Kangkang - Chinese (Simplified, PRC)", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Microsoft Zira - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Microsoft David - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Microsoft Mark - English (United States)", "lang": "en-US", "local_service": True, "is_default": False},
]
MAC_VOICES = [
    {"name": "Tingting", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Samantha", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Alex", "lang": "en-US", "local_service": True, "is_default": False},
    {"name": "Daniel", "lang": "en-GB", "local_service": True, "is_default": False},
]
LINUX_VOICES = [
    {"name": "Google 普通话（中国大陆）", "lang": "zh-CN", "local_service": True, "is_default": False},
    {"name": "Google US English", "lang": "en-US", "local_service": True, "is_default": False},
]

PLATFORM_CFG = {
    "win": {"voices": WINDOWS_VOICES, "platform": "Windows", "platform_value": "Win32",
            "ua_os": "Windows NT 10.0; Win64; x64", "platform_version": "19.0.0",
            "arch": "x86", "bitness": "64",
            "screens": [(1920, 1080, 1.0), (1920, 1080, 1.25), (1920, 1080, 1.5),
                        (2560, 1440, 1.0), (1366, 768, 1.0), (3840, 2160, 1.5)]},
    "mac": {"voices": MAC_VOICES, "platform": "MacIntel", "platform_value": "MacIntel",
            "ua_os": "Macintosh; Intel Mac OS X 10_15_7", "platform_version": "15.0.0",
            "arch": "x86", "bitness": "64",
            "screens": [(1440, 900, 2.0), (2560, 1600, 2.0), (1920, 1080, 1.0),
                        (2880, 1800, 2.0)]},
    "linux": {"voices": LINUX_VOICES, "platform": "Linux x86_64", "platform_value": "Linux x86_64",
              "ua_os": "X11; Linux x86_64", "platform_version": "6.5.0",
              "arch": "x86", "bitness": "64",
              "screens": [(1920, 1080, 1.0), (2560, 1440, 1.0), (1366, 768, 1.0)]},
}

GENERATION_CHROME = {
    "gt": (109, 120), "gtx10": (95, 120), "gtx16": (95, 125),
    "rtx20": (80, 120), "rtx30": (95, 130), "rtx40": (112, 135),
    "rx4": (70, 120), "rx5": (80, 120), "rx6": (100, 130),
    "rx7": (110, 135), "vega": (80, 120), "hd5": (80, 110),
    "hd6": (80, 115), "uhd": (95, 130), "iris": (100, 130), "arc": (110, 135),
}


def gpu_generation(name: str) -> str:
    if name.startswith("gtx16"): return "gtx16"
    if name.startswith("gtx10"): return "gtx10"
    if name.startswith("rtx40"): return "rtx40"
    if name.startswith("rtx30"): return "rtx30"
    if name.startswith("rtx2"): return "rtx20"
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


def _gen_profile(gpu_name: str, gpu: Dict, rng: random.Random, variant: int,
                 platform: str) -> Dict:
    pc = PLATFORM_CFG[platform]
    base_hw = gpu.get("hw_range") or 8
    base_mem = gpu.get("mem_range") or 8
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

    gen = gpu_generation(gpu_name)
    lo, hi = GENERATION_CHROME.get(gen, (95, 130))
    major = rng.randint(lo, hi)
    patch = rng.randint(0, 999)
    build = rng.randint(7000, 7999) if major >= 110 else rng.randint(4000, 6999)
    ua = (f"Mozilla/5.0 ({pc['ua_os']}) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36")
    full_ver = f"{major}.0.{build}.{patch}"

    w, h, dpr = rng.choice(pc["screens"])
    screen = {"width": w, "height": h, "avail_width": w,
              "avail_height": h - (40 if platform == "win" else 24),
              "color_depth": 24, "pixel_depth": 24, "device_pixel_ratio": dpr,
              "color_gamut": "srgb", "dynamic_range_high": False,
              "avail_left": 0, "avail_top": 0}

    renderer = gpu["renderer"]
    m = re.search(r"0x[0-9A-Fa-f]{4,8}", renderer)
    device_id = m.group(0) if m else ""
    vendor = gpu.get("vendor", "").split(" ")[0]
    arch_map = {"NVIDIA": {"rtx40": "ada", "rtx30": "ampere", "rtx20": "turing",
                           "gtx16": "turing", "gtx10": "pascal", "gt": "kepler"},
                "AMD": {"rx7": "rdna3", "rx6": "rdna2", "rx5": "rdna", "rx4": "polaris", "vega": "vega"},
                "Intel": {"arc": "alchemist", "iris": "xe", "uhd": "gen9", "hd": "gen8"}}
    arch = arch_map.get(vendor, {}).get(gpu_generation(gpu_name), "unknown")

    return {
        "name": f"{gpu_name}-v{variant}",
        "notes": renderer[:120],
        "timezone": "Asia/Shanghai", "icu_locale": "zh-CN",
        "navigator": {
            "language": "zh-CN",
            "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "languages": ["zh-CN", "zh", "en-US", "en"],
            "user_agent": ua,
            "platform": pc["platform"], "platform_value": pc["platform_value"],
            "platform_version": pc["platform_version"],
            "hardware_concurrency": hw, "device_memory": mem,
            "vendor": "Google Inc.", "max_touch_points": 0,
        },
        "client_hints": {
            "brand": "Google Chrome", "brand_version": str(major),
            "platform_version": pc["platform_version"],
            "architecture": pc["arch"], "bitness": pc["bitness"], "mobile": False,
            "grease_brand": "Not)A;Brand", "grease_version": "24",
            "chrome_build": build, "chrome_patch": patch,
            "brand_full_version": full_ver, "grease_full_version": f"{major}.0.0.0",
        },
        "screen": screen,
        "window": {"outer_width": w, "outer_height": h - 1,
                   "inner_width": w, "inner_height": h - 136},
        "webgl": {
            "vendor": f"Google Inc. ({vendor})",
            "renderer": renderer,
            "vendor_masked": "WebKit", "renderer_masked": "WebKit WebGL",
            "max_texture_size": gpu.get("max_texture_size") or 16384,
            "max_vertex_attribs": gpu.get("max_vertex_attribs") or 16,
            "extensions": gpu.get("extensions", []),
            "params": gpu.get("webgl_params", {}),
            "shader_precision": gpu.get("shader_precision", {}),
        },
        "webgl2": gpu.get("webgl2", {}),
        "webgpu": {"vendor": vendor.lower(), "architecture": arch,
                   "device": device_id,
                   "description": renderer.split(",")[0].strip(),
                   "limits": _webgpu_limits(arch)},
        "audio": {"sample_rate": 48000 if rng.random() < 0.7 else 44100,
                  "channel_count": 2},
        "connection": {"effective_type": "4g", "downlink_mbps": 10.0,
                       "rtt_msec": 75, "save_data": False},
        "storage_estimate": {"quota_gb": 10},
        "webauthn": {"uvpa": True},
        "memory": {"heap_size_limit": 4294967296},
        "battery": {"charging": True, "level": 1.0, "charging_time": 0,
                    "discharging_time": None},
        "media_devices": {"audio_input_count": 1, "audio_output_count": 1,
                          "video_input_count": 0},
        "speech": {"voices": pc["voices"]},
        "noise": {
            "canvas": {"enabled": True, "seed": rng.randint(1, 2 ** 31)},
            "webgl": {"enabled": False, "seed": 0, "intensity": 0},
            "audio": {"enabled": False, "seed": 0},
            "client_rects": {"enabled": False, "seed": 0, "max_offset": 0},
            "sensors": {"enabled": False, "seed": 0},
            "fonts": {"enabled": False, "seed": 0},
        },
        "tls": {
            "cipher_suites": [4865, 4866, 4867, 49195, 49199, 49196, 49200,
                              52393, 52392, 49171, 49172, 156, 157, 47, 53],
            "signature_algorithms": [1027, 2052, 1025, 1283, 2053, 1281, 2054, 1537],
            "shuffle_extensions": True,
        },
    }


def _webgpu_limits(arch: str) -> Dict:
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
        "minStorageOffsetAlignment": 256, "maxVertexBuffers": 8,
        "maxBufferSize": 268435456, "maxVertexAttributes": 16,
        "maxVertexBufferArrayStride": 2048, "maxInterStageShaderVariables": 15,
        "maxColorAttachments": 8, "maxColorAttachmentBytesPerSample": 32,
        "maxComputeWorkgroupStorageSize": 16384, "maxComputeInvocationsPerWorkgroup": 256,
        "maxComputeWorkgroupSizeX": 256, "maxComputeWorkgroupSizeY": 256,
        "maxComputeWorkgroupSizeZ": 64, "maxComputeWorkgroupsPerDimension": 65535,
    }
    if arch in ("ada", "rdna3", "rdna2", "alchemist"):
        base.update({"maxBufferSize": 2684354560, "maxVertexAttributes": 30,
                     "maxInterStageShaderVariables": 30})
    return base


def generate_profiles(platform: str, table: Dict, variants: int = 3,
                      seed: int | None = None, gpu_names: List[str] | None = None) -> List[Dict]:
    """按能力表生成 profile 列表 (纯函数, 不写文件)。

    Args:
        platform: win | mac | linux
        table:   能力表 dict (gpu_table.json 内容)
        variants: 每型号变体数
        seed:     随机种子 (None = 按当前时间)
        gpu_names: 只生成指定型号 (None = 全部)
    """
    rng = random.Random(seed if seed is not None else datetime.now().timestamp())
    targets = gpu_names or list(table.keys())
    profiles: List[Dict] = []
    for gpu_name in targets:
        gpu = table.get(gpu_name)
        if not gpu:
            continue
        for v in range(1, variants + 1):
            profiles.append(_gen_profile(gpu_name, gpu, rng, v, platform))
    return profiles
