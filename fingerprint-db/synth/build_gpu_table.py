"""
GPU 能力表生成器 — 从本地真实样本提取 30 主流型号的参数基线。

数据来源: 本地缓存的真实设备样本 (参考参数值, GPU 能力为公开硬件规格)。
输出: synth/gpu_table.json — 自建数据库的型号→参数映射核心。
"""
import json
import os
from pathlib import Path

SRC = Path(os.path.expandvars(r"%LOCALAPPDATA%\shardx-sdk\fingerprints"))
OUT = Path(__file__).resolve().parent / "gpu_table.json"

# 30 主流型号 (NVIDIA 14 / AMD 9 / Intel 7)
SELECT = [
    # NVIDIA
    "gt1030", "gtx1050ti", "gtx1060", "gtx1660super",
    "rtx2060", "rtx3060", "rtx3060ti", "rtx3070", "rtx3080", "rtx3090",
    "rtx4060", "rtx4070", "rtx4080", "rtx4090",
    # AMD
    "rx460", "rx480", "rx570", "rx580", "rx6600", "rx6700xt", "rx6800xt",
    "rx7700xt", "vega8",
    # Intel
    "hd520", "hd620", "hd630", "uhd630", "uhd770", "iris-xe", "arc",
]


def load(name: str) -> dict | None:
    p = SRC / f"win-{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    table = {}
    for name in SELECT:
        d = load(name)
        if not d:
            print(f"[skip] {name}: 无样本")
            continue
        w = d.get("webgl", {})
        nav = d.get("navigator", {})
        entry = {
            "renderer": w.get("renderer", ""),
            "vendor": w.get("vendor", ""),
            "webgl_params": {k: v for k, v in w.get("params", {}).items()
                             if not isinstance(v, list) or len(v) <= 4},
            "extensions": w.get("extensions", []),
            "shader_precision": w.get("shader_precision", {}),
            "max_texture_size": w.get("max_texture_size"),
            "max_vertex_attribs": w.get("max_vertex_attribs"),
            "webgl2": {k: v for k, v in (d.get("webgl2") or {}).items()
                       if not isinstance(v, list) or len(v) <= 4},
            "webgpu": {
                "vendor": (d.get("webgpu") or {}).get("vendor", ""),
                "architecture": (d.get("webgpu") or {}).get("architecture", ""),
            },
            "hw_range": nav.get("hardware_concurrency"),
            "mem_range": nav.get("device_memory"),
            "ua": nav.get("user_agent", ""),
            "screen": d.get("screen", {}),
            "audio": d.get("audio", {}),
            "platform_version": (d.get("client_hints") or {}).get("platform_version", ""),
            "brand_full_version": (d.get("client_hints") or {}).get("brand_full_version", ""),
        }
        table[name] = entry
        print(f"[ok] {name}: {w.get('renderer', '?')[:60]} | hw={entry['hw_range']} mem={entry['mem_range']}")

    OUT.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已生成: {OUT} ({len(table)} 型号)")


if __name__ == "__main__":
    main()
