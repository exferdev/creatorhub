# Fingerprint DB — 自建设备指纹数据库

参考 ShardX 思路自建的设备指纹库（真机采集 + 合成生成），与 CreatorHub 浏览器引擎对接。

## 目录结构

```
fingerprint-db/
├── collector/
│   ├── probe.js        # 真机指纹采集脚本 (WebGL 50+参数/UA-CH/屏幕/音频/语音)
│   └── collect.py      # 采集器 (系统 Chrome 运行 probe, 输出 JSON)
├── database/
│   ├── real/           # 真机采集样本 (本机真实基线)
│   └── *.json          # 合成 profile (引擎用)
├── synth/
│   ├── gpu_table.json  # 30 主流 GPU 型号能力表 (renderer/参数/扩展/硬件搭配)
│   ├── build_gpu_table.py  # GPU 表生成器
│   └── generate.py     # 合成器 (型号 → 完整 profile, 一致性规则)
└── validate/
    ├── check.py        # 一致性验证器 (CreepJS Firewall 思路)
    └── integrate_test.py # ShardX 引擎对接测试
```

## 快速使用

```bash
# 1. 真机采集
python collector/collect.py --name auto          # 自动命名 (取 GPU 型号)
python collector/collect.py --name my-pc --repeat 3

# 2. GPU 能力表 (已生成 30 型号, 可扩展)
python synth/build_gpu_table.py

# 3. 合成 profile
python synth/generate.py --count 60              # 60 套
python synth/generate.py --gpu rtx4060 --variants 5   # 单型号多变体

# 4. 一致性校验
python validate/check.py                         # 退出码 0=全过

# 5. ShardX 引擎对接
python validate/integrate_test.py                # 合成指纹真实生效验证
```

## 一致性保证

| 规则 | 说明 |
|------|------|
| renderer 格式 | ANGLE + 设备 ID (0xXXXX), 无乱码/双空格 |
| GPU↔Chrome 时间线 | GT 1030 不配 Chrome 151, RTX 4090 不配 Chrome 100 |
| HW/MEM 搭配 | 新 GPU 不配 4 核, 低端卡不配 32 核, 大核数配大内存 |
| UA↔GPU 平台 | Windows UA 不配 Apple GPU |
| 品牌↔UA | Google Chrome 品牌配 Chrome UA |
| WebGL 参数 | 来自真实基线 (GPU 能力为公开硬件规格) |
| 必备字段 | 对齐 ShardX schema (navigator/client_hints/webgl/webgpu/audio/speech/noise/tls) |

## 对接 ShardX 引擎

合成 profile 为 ShardX 兼容格式, 引擎加载后指纹真实生效:
```
UA一致 ✅ | HW一致 ✅ | GPU一致 ✅ | webdriver=False ✅
```

## 数据来源与合规

- **真机采集**: 在真实系统 Chrome 运行 probe.js 采集 (本机 RTX 4070 Ti SUPER 基线)
- **GPU 能力表**: 30 主流型号的公开硬件规格 (renderer 格式/设备 ID/WebGL 能力/WebGPU limits)
- **合成**: 按一致性规则从基线生成变体
- 完全自主数据, 无第三方库导出/再分发问题

## 路线图

- [x] 真机采集器 (本机样本)
- [x] GPU 能力表 (30 型号)
- [x] 合成器 + 一致性校验
- [x] ShardX 引擎对接
- [ ] 扩充 GPU 型号 (30 → 100+)
- [ ] 多平台 (macOS/Linux 采集基线)
- [ ] 合成 profile 的 BrowserScan 实测
