"""
腾讯云 SCF — serve 函数 (分发)

API 网关触发 (公开读, CDN 前置):
  GET /api/v1/version                  → 版本号 (COS version.json)
  GET /api/v1/profiles?platform=win&since=12  → 增量清单
  GET /api/v1/profiles/{platform}/{name}.json → profile 内容

CDN 缓存规则: profiles/*.json 缓存 3600s, version.json 60s
"""
import json
import os
import re

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


def _json_resp(status: int, obj: dict, cache: int = 0):
    body = json.dumps(obj, ensure_ascii=False)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if cache:
        headers["Cache-Control"] = f"public, max-age={cache}"
    return {"statusCode": status, "headers": headers, "body": body}


def main_handler(event, context):
    path = event.get("path", "")
    q = event.get("queryString", {}) or {}

    # GET /api/v1/version
    if path.endswith("/api/v1/version"):
        ver = _read_json("meta/version.json",
                         {"version": 0, "updated_at": 0, "changelog": ""})
        return _json_resp(200, {"ok": True, **ver}, cache=60)

    # GET /api/v1/profiles?platform=win&since=12
    if path.endswith("/api/v1/profiles"):
        platform = q.get("platform", "win")
        since = int(q.get("since", 0) or 0)
        ver = _read_json("meta/version.json", {"version": 0})
        if ver["version"] <= since:
            return _json_resp(200, {"ok": True, "version": ver["version"],
                                    "updated": False, "profiles": []})
        # 列平台目录下 profile (简化: 直接返回该平台全部文件名)
        files = _cos().list_objects(COS_BUCKET, Prefix=f"profiles/{platform}/",
                                    MaxKeys=1000)
        names = [c["Key"].split("/")[-1] for c in
                 files.get("Contents", []) if c["Key"].endswith(".json")]
        return _json_resp(200, {"ok": True, "version": ver["version"],
                                "updated": True, "profiles": names}, cache=60)

    # GET /api/v1/profiles/{platform}/{name}.json
    m = re.match(r".*/api/v1/profiles/(\w+)/([\w.-]+\.json)$", path)
    if m:
        platform, name = m.group(1), m.group(2)
        data = _read_json(f"profiles/{platform}/{name}", None)
        if data is None:
            return _json_resp(404, {"ok": False, "error": "not found"})
        return {"statusCode": 200,
                "headers": {"Content-Type": "application/json; charset=utf-8",
                            "Cache-Control": "public, max-age=3600"},
                "body": json.dumps(data, ensure_ascii=False)}

    return _json_resp(404, {"ok": False, "error": "unknown path"})
