"""
腾讯云 SCF — ingest 函数 (采集接收 + 校验 + 入库)

API 网关触发: POST /api/v1/samples
请求头: X-API-Key (与函数环境变量 API_KEY 比对)
请求体: probe.js 输出的指纹 JSON

流程:
  1. API key 认证
  2. schema/一致性校验 (lib.validate)
  3. GPU 型号识别 → samples_index.json 记录 (待合成变体 / 待人工确认)
  4. 原始样本存 COS samples/<date>/<hash>.json
"""
import hashlib
import json
import os
import time

from tencentcloud.cos import CosConfig, CosS3Client

# COS 配置 (函数环境变量)
COS_REGION = os.environ.get("COS_REGION", "ap-shanghai")
COS_BUCKET = os.environ.get("COS_BUCKET", "fingerprint-db-1250000000")
API_KEY = os.environ.get("API_KEY", "")

_client = None


def _cos():
    global _client
    if _client is None:
        cfg = CosConfig(
            Region=COS_REGION,
            SecretId=os.environ["COS_SECRET_ID"],
            SecretKey=os.environ["COS_SECRET_KEY"],
        )
        _client = CosS3Client(cfg)
    return _client


def _get_meta():
    """读取 samples_index.json (COS), 不存在则空结构。"""
    try:
        resp = _cos().get_object(COS_BUCKET, "meta/samples_index.json")
        return json.loads(resp["Body"].get_raw_stream().read().decode("utf-8"))
    except Exception:
        return {"samples": [], "pending": []}


def _put_meta(meta: dict):
    _cos().put_object(
        Bucket=COS_BUCKET, Key="meta/samples_index.json",
        Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def _identify_gpu(prof: dict) -> tuple[str, str]:
    """renderer → (型号名, 状态: known|new)。"""
    import re
    renderer = prof.get("webgl", {}).get("renderer", "")
    m = re.search(r"(NVIDIA GeForce [A-Za-z0-9 ]+|AMD Radeon[^,(]*|Intel\(R\) [^,(]+|Apple M\d)", renderer)
    if not m:
        return "unknown", "new"
    # 简化识别: 取 renderer 主名作为型号标识 (SCF 侧由能力表比对决定 known/new)
    return m.group(1).strip().replace(" ", "-").lower()[:40], "new"


def main_handler(event, context):
    """SCF 入口。"""
    # 1. 认证
    headers = event.get("headers", {}) or {}
    if API_KEY and headers.get("X-API-Key") != API_KEY:
        return {"statusCode": 401, "body": json.dumps({"ok": False, "error": "unauthorized"})}

    # 2. 解析 body
    try:
        body = json.loads(event.get("body", "{}"))
    except Exception:
        return {"statusCode": 400, "body": json.dumps({"ok": False, "error": "invalid json"})}

    # 3. 校验
    from lib.validate import validate_profile
    issues = validate_profile(body)
    if issues:
        return {"statusCode": 400, "body": json.dumps(
            {"ok": False, "error": "validation failed", "issues": issues[:10]})}

    # 4. 入库
    sample_hash = hashlib.md5(json.dumps(
        body.get("webgl", {}).get("renderer", "") + str(body.get("navigator", {}).get("hardware_concurrency", ""))
    ).encode()).hexdigest()[:12]
    meta = _get_meta()
    if sample_hash in meta["samples"]:
        return {"statusCode": 200, "body": json.dumps(
            {"ok": True, "duplicate": True, "hash": sample_hash})}

    date = time.strftime("%Y-%m")
    _cos().put_object(
        Bucket=COS_BUCKET, Key=f"samples/{date}/{sample_hash}.json",
        Body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    model, status = _identify_gpu(body)
    record = {"hash": sample_hash, "model": model, "status": status,
              "ts": int(time.time()), "path": f"samples/{date}/{sample_hash}.json"}
    meta["samples"].append(sample_hash)
    meta["pending"].append(record)
    _put_meta(meta)

    return {"statusCode": 200, "body": json.dumps(
        {"ok": True, "hash": sample_hash, "model": model, "status": status,
         "note": "已入库, 待 synth 合成变体"})}
