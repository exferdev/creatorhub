"""
本机定时合成器 — 从 COS 拉新样本 → 校验合并 → 合成 → 回传 profiles/ + version

用法 (Windows 任务计划):
    E:\creatorhub\.venv\Scripts\python.exe E:\creatorhub\fingerprint-db\tencent\synth_timer.py

凭证: tencent/.env.local 或环境变量
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))


def _load_creds() -> dict:
    env = {"TENCENTCLOUD_SECRET_ID": "", "TENCENTCLOUD_SECRET_KEY": "",
           "COS_REGION": "ap-shanghai", "COS_BUCKET": "fingerprint-db-1251558724"}
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k in env:
                env[k] = v
    import os
    for k in env:
        if os.environ.get(k):
            env[k] = os.environ[k]
    if not env["TENCENTCLOUD_SECRET_ID"] or not env["TENCENTCLOUD_SECRET_KEY"]:
        raise SystemExit("[synth] 缺凭证")
    return env


def _cos(creds: dict):
    from qcloud_cos import CosConfig, CosS3Client
    cfg = CosConfig(Region=creds["COS_REGION"],
                    SecretId=creds["TENCENTCLOUD_SECRET_ID"],
                    SecretKey=creds["TENCENTCLOUD_SECRET_KEY"])
    return CosS3Client(cfg)


def _read_json(client, bucket, key, default):
    try:
        resp = client.get_object(bucket, key)
        return json.loads(resp["Body"].get_raw_stream().read().decode("utf-8"))
    except Exception:
        return default


def _write_json(client, bucket, key, obj):
    client.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")


def main():
    creds = _load_creds()
    client = _cos(creds)
    bucket = creds["COS_BUCKET"]

    # 1. 能力表
    tables = {}
    for plat in ("win", "mac", "linux"):
        fname = "gpu_table.json" if plat == "win" else f"gpu_table_{plat}.json"
        tables[plat] = _read_json(client, bucket, f"meta/{fname}", {})

    # 2. 拉取待处理样本 (samples/ 全部, 简单方案: 全量扫描新文件)
    meta = _read_json(client, bucket, "meta/samples_index.json",
                      {"samples": [], "pending": []})
    known = set(meta.get("samples", []))
    new_samples = []
    # 列出 samples/ 目录
    try:
        resp = client.list_objects(bucket, Prefix="samples/", MaxKeys=1000)
        for c in resp.get("Contents", []):
            key = c["Key"]
            if not key.endswith(".json"):
                continue
            h = key.split("/")[-1].replace(".json", "")
            if h not in known:
                new_samples.append(key)
    except Exception as e:
        print(f"[synth] 列 samples 失败: {str(e)[:100]}")

    if not new_samples:
        print(f"[synth] 无新样本 (共 {len(known)} 个)")
        return
    print(f"[synth] 发现 {len(new_samples)} 个新样本")

    # 3. 校验 + 合并到能力表
    from lib.validate import validate_profile
    from lib.generate import generate_profiles
    merged = 0
    for key in new_samples:
        sample = _read_json(client, bucket, key, None)
        if not sample:
            continue
        issues = validate_profile(sample)
        if issues:
            print(f"  [skip] {key}: {issues[:3]}")
            continue
        ua = sample.get("navigator", {}).get("user_agent", "")
        plat = "win" if "Windows" in ua else ("mac" if "Mac OS" in ua else "linux")
        renderer = sample.get("webgl", {}).get("renderer", "")
        name = "sample-" + key.split("/")[-1].replace(".json", "")
        tables[plat][name] = {
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
        known.add(key.split("/")[-1].replace(".json", ""))
        merged += 1

    # 4. 能力表回传 + 全量合成
    for plat, tbl in tables.items():
        fname = "gpu_table.json" if plat == "win" else f"gpu_table_{plat}.json"
        _write_json(client, bucket, f"meta/{fname}", tbl)
    total = 0
    for plat, tbl in tables.items():
        profiles = generate_profiles(plat, tbl, variants=2)
        for p in profiles:
            _write_json(client, bucket, f"profiles/{plat}/{p['name']}.json", p)
            total += 1

    # 5. 版本 ++
    ver = _read_json(client, bucket, "meta/version.json",
                     {"version": 0, "updated_at": 0, "changelog": ""})
    ver["version"] += 1
    ver["updated_at"] = int(time.time())
    ver["changelog"] = f"synth: {merged} 样本合并, {total} profile 重生成"
    _write_json(client, bucket, "meta/version.json", ver)

    # 6. 更新索引
    meta["samples"] = sorted(known)
    meta["pending"] = []
    _write_json(client, bucket, "meta/samples_index.json", meta)
    print(f"[synth] 完成: 合并 {merged}, 生成 {total}, 版本 {ver['version']}")


if __name__ == "__main__":
    main()
