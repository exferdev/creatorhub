"""
腾讯云 SCF — synth 函数 (定时合成)

定时触发 (每日): 处理待合成样本 → 更新能力表 → 重生成 profile → 写 COS → 版本号++

流程:
  1. 读 samples_index.json 待处理样本
  2. 合并样本到能力表 (同型号参数漂移/新型号)
  3. 读现有能力表 (gpu_table) → 合并 → 写回 COS
  4. generate_profiles() 全量重生成 → 写 COS profiles/<platform>/
  5. version.json 版本号++
"""
import json
import os
import time

from tencentcloud.cos import CosConfig, CosS3Client

COS_REGION = os.environ.get("COS_REGION", "ap-shanghai")
COS_BUCKET = os.environ.get("COS_BUCKET", "fingerprint-db-1250000000")

_client = None


def _cos():
    global _client
    if _client is None:
        cfg = CosConfig(Region=COS_REGION,
                        SecretId=os.environ["COS_SECRET_ID"],
                        SecretKey=os.environ["COS_SECRET_KEY"])
        _client = CosS3Client(cfg)
    return _client


def _read_json(key: str, default):
    try:
        resp = _cos().get_object(COS_BUCKET, key)
        return json.loads(resp["Body"].get_raw_stream().read().decode("utf-8"))
    except Exception:
        return default


def _write_json(key: str, obj: dict):
    _cos().put_object(Bucket=COS_BUCKET, Key=key,
                      Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")


def main_handler(event, context):
    # 1. 能力表 (三平台)
    tables = {}
    for plat in ("win", "mac", "linux"):
        fname = "gpu_table.json" if plat == "win" else f"gpu_table_{plat}.json"
        tables[plat] = _read_json(f"meta/{fname}", {})

    # 2. 合并待处理样本 → 能力表 (按 renderer 主名匹配, 简单合并)
    meta = _read_json("meta/samples_index.json", {"samples": [], "pending": []})
    merged = 0
    for rec in meta.get("pending", []):
        sample = _read_json(rec["path"], None)
        if not sample:
            continue
        # 简化: 新型号样本直接作为能力表条目 (SCF 生产版可加强匹配/审核)
        renderer = sample.get("webgl", {}).get("renderer", "")
        if not renderer:
            continue
        plat = "win" if "Windows" in sample.get("navigator", {}).get("user_agent", "") else \
               ("mac" if "Mac OS" in sample.get("navigator", {}).get("user_agent", "") else "linux")
        key = rec.get("model") or f"sample-{rec['hash']}"
        tables[plat][key] = {
            "renderer": renderer,
            "vendor": sample.get("webgl", {}).get("vendor", ""),
            "webgl_params": sample.get("webgl", {}).get("params", {}),
            "extensions": sample.get("webgl", {}).get("extensions", []),
            "shader_precision": sample.get("webgl", {}).get("shader_precision", {}),
            "max_texture_size": sample.get("webgl", {}).get("max_texture_size"),
            "max_vertex_attribs": sample.get("webgl", {}).get("max_vertex_attribs"),
            "webgl2": sample.get("webgl2", {}),
            "hw_range": sample.get("navigator", {}).get("hardware_concurrency"),
            "mem_range": sample.get("navigator", {}).get("device_memory"),
        }
        merged += 1
    # 能力表写回 COS
    for plat, tbl in tables.items():
        fname = "gpu_table.json" if plat == "win" else f"gpu_table_{plat}.json"
        _write_json(f"meta/{fname}", tbl)

    # 3. 重生成 profile (每平台 2 变体)
    from lib.generate import generate_profiles
    total = 0
    for plat, tbl in tables.items():
        profiles = generate_profiles(plat, tbl, variants=2)
        for p in profiles:
            _write_json(f"profiles/{plat}/{p['name']}.json", p)
            total += 1

    # 4. 版本号 ++
    ver = _read_json("meta/version.json", {"version": 0, "updated_at": 0, "changelog": ""})
    ver["version"] += 1
    ver["updated_at"] = int(time.time())
    ver["changelog"] = f"synth: {merged} 样本合并, {total} profile 重生成"
    _write_json("meta/version.json", ver)

    # 5. 清空待处理
    meta["pending"] = []
    _write_json("meta/samples_index.json", meta)

    return {"ok": True, "merged": merged, "profiles": total, "version": ver["version"]}
