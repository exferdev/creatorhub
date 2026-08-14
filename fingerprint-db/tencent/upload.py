"""
采集端上报工具 — 真机采集 → COS 直传 (方案 A, 零 SCF 依赖)

用法:
    python tencent/upload.py [--name auto] [--repeat 1]

流程: probe.js 本地采集 → lib.validate 预校验 → COS SDK 直传 samples/YYYY-MM/
凭证: tencent/.env.local (TENCENTCLOUD_SECRET_ID/KEY) 或环境变量
"""
import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PROJECT / "collector"))


def _load_creds() -> dict:
    """读取 .env.local / 环境变量凭证。"""
    env = {
        "TENCENTCLOUD_SECRET_ID": "",
        "TENCENTCLOUD_SECRET_KEY": "",
        "COS_REGION": "ap-shanghai",
        "COS_BUCKET": "fingerprint-db-1251558724",
    }
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
        raise SystemExit("[upload] 缺凭证: 填 tencent/.env.local 或环境变量")
    return env


def _cos_client(creds: dict):
    from qcloud_cos import CosConfig, CosS3Client
    cfg = CosConfig(Region=creds["COS_REGION"],
                    SecretId=creds["TENCENTCLOUD_SECRET_ID"],
                    SecretKey=creds["TENCENTCLOUD_SECRET_KEY"])
    return CosS3Client(cfg)


def collect_local(name: str, repeat: int = 1) -> dict:
    """复用 collector/collect.py 采集。"""
    from collect import collect
    return asyncio.run(collect(name, repeat))


def validate_local(prof: dict) -> list:
    from lib.validate import validate_profile
    return validate_profile(prof)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="auto")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--skip-local-validate", action="store_true")
    args = ap.parse_args()
    creds = _load_creds()

    print(f"[upload] 采集 (name={args.name})...")
    prof = collect_local(args.name, args.repeat)
    print(f"  采集完成: GPU={prof['webgl']['renderer'][:50]}")

    if not args.skip_local_validate:
        issues = validate_local(prof)
        if issues:
            print(f"[upload] 本地校验未通过: {issues[:5]}")
            sys.exit(1)
        print("[upload] 本地校验通过")

    # COS 直传 samples/YYYY-MM/<hash>.json
    sample_hash = hashlib.md5(
        (prof["webgl"]["renderer"] + str(prof["navigator"]["hardware_concurrency"])).encode()
    ).hexdigest()[:12]
    date = time.strftime("%Y-%m")
    key = f"samples/{date}/{sample_hash}.json"
    client = _cos_client(creds)
    client.put_object(Bucket=creds["COS_BUCKET"], Key=key,
                      Body=json.dumps(prof, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")
    print(f"[upload] 已上传: cos://{creds['COS_BUCKET']}/{key}")
    print("[upload] 等待本机 synth_timer 合并入库")


if __name__ == "__main__":
    main()
