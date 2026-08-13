"""
腾讯云 SCF 复用 — profile 一致性校验纯函数。

从 fingerprint-db/validate/check.py 抽出, 无 CLI 依赖, 可被 SCF ingest 直接 import。
校验规则 (CreepJS Firewall / BrowserScan checkOsConsistency 思路):
  renderer 格式 / GPU↔Chrome 时间线 / HW↔MEM / UA↔平台 / 品牌↔UA / 参数基线 / schema
"""
import re
from typing import Dict, List

GENERATION_CHROME = {
    "gt": (109, 120), "gtx10": (95, 120), "gtx16": (95, 125),
    "rtx20": (80, 120), "rtx30": (95, 130), "rtx40": (112, 135),
    "rx4": (70, 120), "rx5": (80, 120), "rx6": (100, 130),
    "rx7": (110, 135), "vega": (80, 120), "hd5": (80, 110),
    "hd6": (80, 115), "uhd": (95, 130), "iris": (100, 130), "arc": (110, 135),
}

REQUIRED_FIELDS = ["navigator", "client_hints", "screen", "window", "webgl",
                   "audio", "speech", "storage_estimate", "noise", "tls"]


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


def validate_profile(prof: Dict, gpu_table: Dict | None = None) -> List[str]:
    """校验单套 profile, 返回问题列表 (空 = 通过)。gpu_table 为能力表(可选, 校验参数基线)。"""
    issues: List[str] = []
    name = prof.get("name", "?")
    # ── schema 完整 ──
    for k in REQUIRED_FIELDS:
        if k not in prof:
            issues.append(f"缺字段: {k}")
    if "webgl" not in prof or not prof["webgl"].get("renderer"):
        issues.append("缺 webgl.renderer")
        return issues
    # ── renderer 格式 (平台感知) ──
    r = prof["webgl"]["renderer"]
    ua = prof.get("navigator", {}).get("user_agent", "")
    is_win = "Windows" in ua
    is_apple = "Apple" in r
    is_linux = "X11; Linux" in ua
    if is_win:
        if not r.startswith("ANGLE ("):
            issues.append(f"Windows renderer 非 ANGLE 格式: {r[:50]}")
        if not re.search(r"0x[0-9A-Fa-f]{4,8}", r):
            issues.append(f"renderer 缺设备 ID: {r[:50]}")
    elif is_linux:
        pass  # Linux DRM/LLVMpipe/ANGLE 均真实
    elif not is_apple:
        if not r.startswith("ANGLE (") and not r.startswith("Apple"):
            issues.append(f"renderer 格式异常: {r[:50]}")
    if re.search(r"\s{2,}", r):
        issues.append(f"renderer 双空格: {r[:50]}")
    # ── GPU↔Chrome 时间线 ──
    m = re.search(r"Chrome/(\d+)", ua)
    if m:
        gen = gpu_generation(name)
        lo, hi = GENERATION_CHROME.get(gen, (95, 130))
        major = int(m.group(1))
        if not (lo <= major <= hi):
            issues.append(f"GPU={name}(世代{gen}) 配 Chrome {major} (合理 {lo}-{hi})")
    # ── HW/MEM 搭配 ──
    nav = prof.get("navigator", {})
    hw = nav.get("hardware_concurrency", 0)
    mem = nav.get("device_memory", 0)
    if hw >= 20 and mem < 16:
        issues.append(f"{hw} 核配 {mem}GB 内存不合理")
    # ── UA↔平台 ──
    plat = nav.get("platform_value", "")
    if "Windows" in ua and "Win32" not in plat:
        issues.append(f"Windows UA 配 platform_value={plat}")
    if "Mac OS" in ua and plat != "MacIntel":
        issues.append(f"macOS UA 配 platform_value={plat}")
    if "X11; Linux" in ua and "Linux" not in plat:
        issues.append(f"Linux UA 配 platform_value={plat}")
    if "Windows" in ua and "Apple" in r:
        issues.append("Windows UA 配 Apple GPU")
    # ── 品牌↔UA ──
    ch = prof.get("client_hints", {})
    if "Edg/" in ua and ch.get("brand") != "Microsoft Edge":
        issues.append("Edge UA 配 Google Chrome 品牌")
    # ── 参数基线 (可选) ──
    if gpu_table:
        base = gpu_table.get(name)
        if base:
            if prof["webgl"].get("max_texture_size") != base.get("max_texture_size"):
                if base.get("max_texture_size") is not None:
                    issues.append(f"{name} max_texture_size 与基线不符")
            bp = base.get("webgl_params", {})
            pp = prof["webgl"].get("params", {})
            for k in ("MAX_TEXTURE_SIZE", "MAX_VERTEX_ATTRIBS", "SUBPIXEL_BITS"):
                if k in bp and k in pp and pp[k] != bp[k]:
                    issues.append(f"{name} {k}: {pp[k]} ≠ 基线 {bp[k]}")
    # ── audio / speech ──
    if prof.get("audio", {}).get("sample_rate") not in (44100, 48000):
        issues.append(f"audio sample_rate 异常: {prof.get('audio', {}).get('sample_rate')}")
    if not prof.get("speech", {}).get("voices"):
        issues.append("speech voices 为空")
    return issues
