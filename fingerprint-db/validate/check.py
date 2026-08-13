"""
指纹一致性验证器 — 遍历生成的 profile, 校验内部一致性与参数分布。

检查项 (参考 CreepJS Firewall / BrowserScan checkOsConsistency):
  1. renderer 字符串格式 (ANGLE + 设备 ID + 无乱码)
  2. GPU 世代 ↔ Chrome 版本 时间线一致 (老卡不配新浏览器)
  3. hw/mem 搭配合理 (新 GPU 不配 4 核, 低端卡不配 32 核)
  4. UA 平台 ↔ GPU 平台一致 (Windows UA 不配 Apple GPU)
  5. 品牌 ↔ UA 一致 (Google Chrome 品牌配 Chrome UA)
  6. WebGL 参数与真实基线一致 (参数值 = 基线值, 天然落真实分布)
  7. 必备字段完整性 (对齐 ShardX schema)

用法: python validate/check.py [--db ../database]
退出码: 0=全过, 1=有异常
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = json.loads((ROOT / "synth" / "gpu_table.json").read_text(encoding="utf-8"))

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


def check_renderer(prof: dict, issues: list):
    r = prof["webgl"]["renderer"]
    if not r.startswith("ANGLE ("):
        issues.append(f"renderer 非 ANGLE 格式: {r[:50]}")
    if not re.search(r"0x[0-9A-Fa-f]{4,8}", r):
        issues.append(f"renderer 缺设备 ID: {r[:50]}")
    if re.search(r"\s{2,}", r):
        issues.append(f"renderer 双空格: {r[:50]}")


def check_gpu_chrome(prof: dict, issues: list):
    name = prof["name"].rsplit("-v", 1)[0]
    gen = gpu_generation(name)
    lo, hi = GENERATION_CHROME.get(gen, (95, 130))
    m = re.search(r"Chrome/(\d+)", prof["navigator"]["user_agent"])
    if not m:
        issues.append("UA 缺 Chrome 版本")
        return
    major = int(m.group(1))
    if not (lo <= major <= hi):
        issues.append(f"GPU={name}(世代{gen}) 配 Chrome {major} (合理 {lo}-{hi})")


def check_hw_mem(prof: dict, issues: list):
    name = prof["name"].rsplit("-v", 1)[0]
    hw = prof["navigator"]["hardware_concurrency"]
    mem = prof["navigator"]["device_memory"]
    base_hw = TABLE.get(name, {}).get("hw_range") or 8
    base_mem = TABLE.get(name, {}).get("mem_range") or 8
    if base_hw <= 4 and hw > 8:
        issues.append(f"{name}(基线{base_hw}核) 配 {hw} 核过高")
    if base_hw >= 16 and hw <= 6:
        issues.append(f"{name}(基线{base_hw}核) 配 {hw} 核过低")
    if hw >= 20 and mem < 16:
        issues.append(f"{hw} 核配 {mem}GB 内存不合理")


def check_os_consistency(prof: dict, issues: list):
    ua = prof["navigator"]["user_agent"]
    renderer = prof["webgl"]["renderer"]
    if "Windows" in ua and "Apple" in renderer:
        issues.append("Windows UA 配 Apple GPU")
    if "Mac OS" in ua and "NVIDIA" in renderer:
        issues.append("macOS UA 配 NVIDIA GPU (Windows 场景不适用)")
    # 品牌 ↔ UA
    if "Edg/" in ua and prof.get("client_hints", {}).get("brand") != "Microsoft Edge":
        issues.append("Edge UA 配 Google Chrome 品牌")


def check_params_baseline(prof: dict, issues: list):
    """WebGL 参数与真实基线一致 (合成的参数来自基线 → 落真实分布)。"""
    name = prof["name"].rsplit("-v", 1)[0]
    base = TABLE.get(name, {})
    if not base:
        return
    p = prof["webgl"].get("params", {})
    bp = base.get("webgl_params", {})
    for k in ("MAX_TEXTURE_SIZE", "MAX_VERTEX_ATTRIBS", "MAX_CUBE_MAP_TEXTURE_SIZE",
              "MAX_RENDERBUFFER_SIZE", "MAX_VIEWPORT_DIMS", "SUBPIXEL_BITS"):
        if k in bp and k in p and p[k] != bp[k]:
            issues.append(f"{name} {k}: 合成 {p[k]} ≠ 基线 {bp[k]}")
    if prof["webgl"].get("max_texture_size") != base.get("max_texture_size"):
        issues.append(f"{name} max_texture_size 与基线不符")


def check_schema(prof: dict, issues: list):
    required = ["navigator", "client_hints", "screen", "window", "webgl", "audio",
                "speech", "storage_estimate", "noise", "tls"]
    for k in required:
        if k not in prof:
            issues.append(f"缺字段: {k}")
    if not prof["speech"].get("voices"):
        issues.append("speech voices 为空")
    if prof["audio"].get("sample_rate") not in (44100, 48000):
        issues.append(f"audio sample_rate 异常: {prof['audio'].get('sample_rate')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "database"))
    args = ap.parse_args()
    files = sorted(Path(args.db).glob("*.json"))
    # 排除真机采集的原始样本 (无 -v 后缀且为首次采集格式, 也一并校验)
    total_issues = 0
    for f in files:
        prof = json.loads(f.read_text(encoding="utf-8"))
        if "webgl" not in prof:
            continue
        issues = []
        check_renderer(prof, issues)
        check_gpu_chrome(prof, issues)
        check_hw_mem(prof, issues)
        check_os_consistency(prof, issues)
        check_params_baseline(prof, issues)
        check_schema(prof, issues)
        if issues:
            total_issues += len(issues)
            print(f"[FAIL] {prof['name']}: {issues[:4]}")
    if total_issues == 0:
        print(f"[PASS] {len(files)} 套 profile 全部通过一致性校验")
    else:
        print(f"[FAIL] 共 {total_issues} 个问题")
        sys.exit(1)


if __name__ == "__main__":
    main()
