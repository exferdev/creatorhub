"""
采集端上报工具 — 真机采集 → 上传到腾讯云 fingerprint-db 服务。

用法:
    python tencent/upload.py --key <API_KEY> --url https://xxx.apigw.tencentcs.com
    python tencent/upload.py --key <API_KEY> --name auto

流程: 本地 probe.js 采集 → 本地预校验 (lib.validate) → POST ingest API
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tencent"))
sys.path.insert(0, str(ROOT / "collector"))

import urllib.request  # noqa: E402


def collect_local(name: str, repeat: int = 1) -> dict:
    """本地采集 (复用 collector/collect.py 逻辑简化版: probe.js + 系统 Chrome)。"""
    import asyncio
    from collect import collect
    return asyncio.run(collect(name, repeat))


def validate_local(prof: dict) -> list:
    from lib.validate import validate_profile
    return validate_profile(prof)


def upload(prof: dict, api_url: str, api_key: str) -> dict:
    body = json.dumps(prof, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/api/v1/samples", data=body,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="服务端 API key")
    ap.add_argument("--url", default="https://xxx.apigw.tencentcs.com",
                    help="API 网关地址 (不含 /api 前缀)")
    ap.add_argument("--name", default="auto")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--skip-local-validate", action="store_true")
    args = ap.parse_args()

    print(f"[upload] 采集 (name={args.name}, repeat={args.repeat})...")
    prof = collect_local(args.name, args.repeat)
    print(f"  采集完成: GPU={prof['webgl']['renderer'][:50]}")

    if not args.skip_local_validate:
        issues = validate_local(prof)
        if issues:
            print(f"[upload] 本地校验未通过: {issues[:5]}")
            sys.exit(1)
        print("[upload] 本地校验通过")

    result = upload(prof, args.url, args.key)
    print(f"[upload] 上报结果: {json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
