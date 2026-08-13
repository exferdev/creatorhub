# 腾讯云版部署与使用说明

fingerprint-db 的腾讯云部署版：**SCF（Python 校验/合成）+ COS（存储）+ CDN（分发）**，与本地 CLI 版解耦。

## 架构

```
采集端 (真机 probe.js)
  → upload.py → SCF ingest (校验闸门, 复用 lib/validate)
  → COS samples/ + samples_index.json
  → SCF synth (Cron 每日, 复用 lib/generate)
  → COS profiles/ + version.json
  → CDN 分发 → CreatorHub 拉取
```

## 目录

```
tencent/
├── lib/
│   ├── validate.py        # validate_profile() 纯函数 (SCF 校验闸门)
│   ├── generate.py        # generate_profiles() 纯函数 (SCF 合成器)
│   └── gpu_tables/        # 能力表 JSON (win/mac/linux)
├── funcs/
│   ├── ingest.py          # SCF: POST /api/v1/samples (API key + 校验 + COS 入库)
│   ├── synth.py           # SCF Cron: 合并样本 → 重生成 → COS → 版本++
│   └── serve.py           # SCF: version/profiles 分发 (CDN 缓存头)
├── deploy/
│   └── serverless.yml     # Serverless Framework 部署配置
├── upload.py              # 采集端上报工具 (采集+本地校验+POST)
├── requirements.txt
└── README.md
```

## 部署步骤

### 1. 腾讯云准备
- 开通 COS（创建 bucket `fingerprint-db-1250000000`，区域 ap-shanghai）
- 创建 API 密钥（`COS_SECRET_ID` / `COS_SECRET_KEY`）
- 域名备案（CDN/API 网关绑定需备案域名）

### 2. COS 初始化
```bash
# bucket 结构:
#   profiles/win|mac|linux/*.json   # 合成 profile
#   samples/YYYY-MM/*.json          # 采集样本
#   meta/gpu_table*.json            # 能力表
#   meta/version.json               # {version, updated_at, changelog}
#   meta/samples_index.json         # {samples: [hash], pending: [记录]}
# 首次: 上传 fingerprint-db/database/*.json 到 profiles/ (或等 synth 生成)
```

### 3. 部署 SCF
```bash
# 方式 A: Serverless Framework
npm install -g serverless
sls deploy --stage prod

# 方式 B: SCF 控制台
# 1. 新建 3 个函数 (Python3.9, 超时 60s): ingest / synth / serve
# 2. 上传代码 (tencent/ 目录, 含 lib/funcs/requirements.txt)
# 3. 环境变量: COS_REGION / COS_BUCKET / COS_SECRET_ID / COS_SECRET_KEY / API_KEY
# 4. 触发器: ingest→API网关 POST /api/v1/samples
#           synth→定时 (每日 03:00)
#           serve→API网关 GET /api/v1/{proxy+}
```

### 4. CDN 配置
```
域名: fingerprint-db.example.com
源站: COS bucket
缓存规则:
  /api/v1/version                    → 60s
  /api/v1/profiles/*                 → 3600s
  profiles/*.json                    → 3600s
回源: 直接访问 COS (serve 也可走 API 网关)
```

## API

```bash
# 采集上报 (采集端)
curl -X POST https://api.fingerprint-db.com/api/v1/samples \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d @sample.json

# 分发 (CreatorHub)
curl https://cdn.fingerprint-db.com/api/v1/version
curl "https://cdn.fingerprint-db.com/api/v1/profiles?platform=win&since=12"
curl https://cdn.fingerprint-db.com/api/v1/profiles/win/rtx4060-v1.json
```

## 采集端

```bash
python tencent/upload.py --key <API_KEY> --url https://api.fingerprint-db.com
```

## 成本 (个人规模)

| 资源 | 月成本 |
|------|:---:|
| SCF 调用 (低频) | ≈ ¥0 (免费额度内) |
| COS 存储 (3MB) | ≈ ¥0 |
| CDN 流量 | ≈ ¥0 |
| 合计 | ≈ ¥0 |

## 本地测试 (无需云资源)

```bash
# 纯函数直接跑
python -c "from tencent.lib.validate import validate_profile; print(validate_profile(open('fingerprint-db/database/rtx4060-v1.json').read()))"

# 合成器本地验证
python -c "
import json
from tencent.lib.generate import generate_profiles
table = json.load(open('tencent/lib/gpu_tables/gpu_table.json'))
ps = generate_profiles('win', table, variants=2)
print(len(ps), 'profiles generated')"
```

## 说明

- **lib/* 为纯函数**：`validate_profile(prof)` / `generate_profiles(platform, table, ...)`，与 CLI 版 (validate/check.py, synth/generate.py) 逻辑一致，SCF 直接复用
- 生产化待办：新型号人工审核（管理端）、采集限流、能力表合并的型号匹配增强
