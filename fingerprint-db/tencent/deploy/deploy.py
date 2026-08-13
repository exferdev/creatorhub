"""
腾讯云部署辅助 — COS 初始化 + SCF 函数创建/更新 + 部署摘要

前置 (环境变量):
  TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY  腾讯云 API 密钥
  FINGERPRINT_API_KEY                                采集端 API key (自定义)
  COS_SECRET_ID / COS_SECRET_KEY                     建议与 API 密钥一致 (SCF 内访问 COS)

用法:
  pip install -r tencent/requirements.txt
  python tencent/deploy/deploy.py --region ap-shanghai --bucket fingerprint-db-1250000000

说明:
  - COS: 创建 bucket + 目录结构 + 初始 meta (能力表/version/samples_index)
  - SCF: 打包 tencent/ (lib+funcs) 为 zip, 创建/更新 ingest/synth/serve 三函数
  - API 网关触发器: 创建后请按输出指引在控制台绑定 (或 tccli scf CreateTrigger)
"""
import argparse
import io
import json
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # tencent/
PROJECT = ROOT.parent                          # fingerprint-db/


def check_env() -> dict:
    env = {
        "TENCENTCLOUD_SECRET_ID": os.environ.get("TENCENTCLOUD_SECRET_ID", ""),
        "TENCENTCLOUD_SECRET_KEY": os.environ.get("TENCENTCLOUD_SECRET_KEY", ""),
        "FINGERPRINT_API_KEY": os.environ.get("FINGERPRINT_API_KEY", "dev-key"),
    }
    if not env["TENCENTCLOUD_SECRET_ID"] or not env["TENCENTCLOUD_SECRET_KEY"]:
        raise SystemExit("[deploy] 缺少 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY "
                         "(腾讯云控制台→访问管理→API密钥管理)")
    return env


def build_code_zip() -> bytes:
    """打包 tencent/ 为 SCF 代码包 (lib + funcs + requirements.txt)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for base in ("lib", "funcs"):
            for f in (ROOT / base).rglob("*"):
                if f.is_file() and "__pycache__" not in str(f):
                    z.write(f, f.relative_to(ROOT).as_posix())
        req = ROOT / "requirements.txt"
        z.write(req, "requirements.txt")
    return buf.getvalue()


def init_cos(bucket: str, region: str, env: dict):
    """COS bucket 初始化 + 初始 meta 上传。"""
    from qcloud_cos import CosConfig, CosS3Client
    cfg = CosConfig(Region=region,
                    SecretId=env["TENCENTCLOUD_SECRET_ID"],
                    SecretKey=env["TENCENTCLOUD_SECRET_KEY"])
    client = CosS3Client(cfg)
    # 1. bucket 存在检查/创建
    try:
        client.head_bucket(Bucket=bucket)
        print(f"[COS] bucket 已存在: {bucket}")
    except Exception:
        client.create_bucket(Bucket=bucket, BucketConfig={"BucketAZConfig": "MAZ"})
        print(f"[COS] 创建 bucket: {bucket}")
    # 2. 目录占位 (COS 无目录概念, 写一个占位文件)
    client.put_object(Bucket=bucket, Key="profiles/win/",
                      Body=b"", ContentType="application/x-directory")
    client.put_object(Bucket=bucket, Key="profiles/mac/",
                      Body=b"", ContentType="application/x-directory")
    client.put_object(Bucket=bucket, Key="profiles/linux/",
                      Body=b"", ContentType="application/x-directory")
    # 3. meta: 能力表 (win/mac/linux)
    for fname in ("gpu_table.json", "gpu_table_mac.json", "gpu_table_linux.json"):
        src = PROJECT / "synth" / fname
        if src.exists():
            client.put_object(Bucket=bucket, Key=f"meta/{fname}",
                              Body=src.read_bytes(), ContentType="application/json")
            print(f"[COS] 上传 meta/{fname}")
    # 4. meta: version + samples_index
    client.put_object(Bucket=bucket, Key="meta/version.json",
                      Body=json.dumps({"version": 0, "updated_at": 0, "changelog": "init"}).encode(),
                      ContentType="application/json")
    client.put_object(Bucket=bucket, Key="meta/samples_index.json",
                      Body=json.dumps({"samples": [], "pending": []}).encode(),
                      ContentType="application/json")
    print("[COS] 初始化完成 (meta/version.json, meta/samples_index.json)")


def deploy_functions(bucket: str, region: str, env: dict):
    """创建/更新 3 个 SCF 函数。"""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.scf.v20180416 import scf_client, models

    cred = credential.Credential(env["TENCENTCLOUD_SECRET_ID"],
                                 env["TENCENTCLOUD_SECRET_KEY"])
    hp = HttpProfile(); hp.endpoint = "scf.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = hp
    client = scf_client.ScfClient(cred, region, cp)
    code_zip = build_code_zip()

    common_env = {
        "COS_REGION": region, "COS_BUCKET": bucket,
        "COS_SECRET_ID": env["TENCENTCLOUD_SECRET_ID"],
        "COS_SECRET_KEY": env["TENCENTCLOUD_SECRET_KEY"],
        "API_KEY": env["FINGERPRINT_API_KEY"],
    }

    for name, handler, timeout in (
        ("ingest", "funcs.ingest.main_handler", 60),
        ("synth", "funcs.synth.main_handler", 300),
        ("serve", "funcs.serve.main_handler", 60),
    ):
        req = models.CreateFunctionRequest()
        req.FunctionName = name
        req.Handler = handler
        req.Runtime = "Python3.9"
        req.Timeout = timeout
        req.MemorySize = 128
        req.Environment = {"Variables": common_env}
        req.Code = {"ZipFile": code_zip}
        try:
            resp = client.CreateFunction(req)
            print(f"[SCF] 创建 {name}: {resp.RequestId}")
        except Exception as e:
            if "FunctionNameInUse" in str(e):
                # 已存在 → 更新代码
                upd = models.UpdateFunctionCodeRequest()
                upd.FunctionName = name
                upd.Handler = handler
                upd.Code = {"ZipFile": code_zip}
                client.UpdateFunctionCode(upd)
                ue = models.UpdateFunctionConfigurationRequest()
                ue.FunctionName = name
                ue.Timeout = timeout
                ue.Environment = {"Variables": common_env}
                client.UpdateFunctionConfiguration(ue)
                print(f"[SCF] 更新 {name}")
            else:
                print(f"[SCF] {name} 失败: {str(e)[:120]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-shanghai")
    ap.add_argument("--bucket", default="fingerprint-db-1250000000")
    args = ap.parse_args()
    env = check_env()
    print(f"[deploy] 区域={args.region} bucket={args.bucket}")
    init_cos(args.bucket, args.region, env)
    deploy_functions(args.bucket, args.region, env)
    print("\n=== 部署完成 ===")
    print("1. API 网关绑定 (控制台):")
    print("   ingest → POST /api/v1/samples (或控制台自动生成路径)")
    print("   serve  → GET  /api/v1/{proxy+}")
    print("2. CDN 绑定 (控制台→CDN): 域名指向 COS bucket, 缓存规则见 README")
    print("3. 采集端: python tencent/upload.py --key <FINGERPRINT_API_KEY> --url <网关地址>")


if __name__ == "__main__":
    main()
