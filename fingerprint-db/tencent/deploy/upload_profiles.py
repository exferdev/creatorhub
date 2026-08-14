"""一次性: 本地 database/ 434 套 profile 批量上传到 COS profiles/ (按平台分类)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\creatorhub\fingerprint-db\tencent")
from upload import _load_creds, _cos_client

DB = Path(r"E:\creatorhub\fingerprint-db\database")

def platform_of(prof: dict) -> str:
    pv = prof.get("navigator", {}).get("platform_value", "")
    if "Win32" in pv:
        return "win"
    if "Mac" in pv:
        return "mac"
    if "Linux" in pv:
        return "linux"
    return "win"

def main():
    creds = _load_creds()
    client = _cos_client(creds)
    bucket = creds["COS_BUCKET"]
    files = sorted(DB.glob("*.json"))
    # 跳过 real/ 子目录 (已排除 by glob 根目录)
    ok = 0
    for f in files:
        prof = json.loads(f.read_text(encoding="utf-8"))
        if "webgl" not in prof:
            continue
        plat = platform_of(prof)
        key = f"profiles/{plat}/{f.name}"
        client.put_object(Bucket=bucket, Key=key,
                          Body=f.read_bytes(), ContentType="application/json")
        ok += 1
    print(f"上传完成: {ok} 套 → cos://{bucket}/profiles/")

if __name__ == "__main__":
    main()
