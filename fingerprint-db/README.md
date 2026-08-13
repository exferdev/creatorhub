# Fingerprint DB — 自建设备指纹数据库

参考 ShardX 思路自建的**完全自主**设备指纹库（真机采集 + 合成生成），与 CreatorHub 抖音浏览器引擎（ShardX）对接，实现多账号引擎级指纹差异化且无痕（BrowserScan 实测通过）。

## 规模

| 平台 | GPU 型号 | 合成 profile |
|------|:---:|:---:|
| Windows | 120 | 360 |
| macOS（含 M1-M5 Apple Silicon） | 31 | 62 |
| Linux | 19 | 38 |
| **合计** | **170** | **434** |

## 目录结构

```
fingerprint-db/
├── collector/
│   ├── probe.js        # 真机指纹采集脚本 (WebGL 50+参数/UA-CH/屏幕/音频/语音/WebGPU)
│   └── collect.py      # 采集器 (系统 Chrome 运行 probe, 输出 JSON)
├── database/
│   ├── real/           # 真机采集样本 (本机 RTX 4070 Ti SUPER 真实基线)
│   └── *.json          # 合成 profile (引擎用, 434 套跨平台)
├── synth/
│   ├── gpu_table.json       # Windows GPU 能力表 (120 型号)
│   ├── gpu_table_mac.json   # macOS GPU 能力表 (31 型号)
│   ├── gpu_table_linux.json # Linux GPU 能力表 (19 型号)
│   ├── build_gpu_table.py   # GPU 表生成器 (--platform win/mac/linux, --all 全量)
│   └── generate.py          # 合成器 (平台感知, 一致性规则)
└── validate/
    ├── check.py             # 一致性验证器 (CreepJS Firewall 思路, 平台感知)
    ├── integrate_test.py    # ShardX 引擎对接测试
    └── browserscan_test.py  # BrowserScan 实测 (合成 profile 端到端)
```

## 快速使用

```bash
# 1. 真机采集 (需要真实 Chrome)
python collector/collect.py --name auto          # 自动命名 (取 GPU 型号)
python collector/collect.py --name my-pc --repeat 3

# 2. GPU 能力表 (已生成 170 型号: win/mac/linux 三表)
python synth/build_gpu_table.py --all --platform win     # 全量 Windows
python synth/build_gpu_table.py --all --platform mac     # macOS
python synth/build_gpu_table.py --all --platform linux   # Linux

# 3. 合成 profile (平台感知)
python synth/generate.py --count 0 --variants 3 --platform win    # 360 套
python synth/generate.py --count 0 --variants 2 --platform mac    # 62 套
python synth/generate.py --count 0 --variants 2 --platform linux  # 38 套
python synth/generate.py --gpu rtx4060 --variants 5 --platform win  # 单型号多变体

# 4. 一致性校验 (434 套全过)
python validate/check.py                         # 退出码 0=全过

# 5. ShardX 引擎对接 + BrowserScan 实测
python validate/integrate_test.py                # 合成指纹真实生效验证
python validate/browserscan_test.py              # BrowserScan 三项检测实测
```

## 平台感知

| 平台 | UA | renderer 格式 | 语音 |
|------|-----|--------------|------|
| Windows | `Windows NT 10.0; Win64` | ANGLE Direct3D（含设备 ID） | SAPI 中文/英文 |
| macOS | `Macintosh; Intel Mac OS X` | ANGLE / Apple Metal（M 系） | Apple 系统语音 |
| Linux | `X11; Linux x86_64` | ANGLE Vulkan / DRM / LLVMpipe | espeak/Google |

## 一致性保证

| 规则 | 说明 |
|------|------|
| renderer 格式 | Windows: ANGLE + 设备 ID (0xXXXX) 严格校验; macOS 允许 Metal; Linux 允许 DRM/LLVMpipe |
| GPU↔Chrome 时间线 | GT 1030 不配 Chrome 151, RTX 4090 不配 Chrome 100 |
| HW/MEM 搭配 | 新 GPU 不配 4 核, 低端卡不配 32 核, 大核数配大内存 |
| UA↔GPU 平台 | Windows UA 不配 Apple GPU, macOS 不配 NVIDIA 独显 |
| 品牌↔UA | Google Chrome 品牌配 Chrome UA |
| WebGL 参数 | 来自真实基线 (GPU 能力为公开硬件规格) |
| 必备字段 | 对齐 ShardX schema (navigator/client_hints/webgl/webgpu/audio/speech/noise/tls) |

## CreatorHub 集成

抖音账号浏览器启动时按 `fp_seed` 从 `database/` 确定性选择 profile（`app/browser/manager.py::_pick_custom_profile`）：

```
fp_seed → fingerprint-db/database (434 套, 确定性索引)
        → ShardX 引擎加载 (合成 profile 格式兼容)
        → 浏览器指纹生效 (同账号每次同指纹)
        → 回退: ShardX 自带库 (.shardx_id 持久化)
```

## 验证结果

### ShardX 引擎对接（`integrate_test.py`）

```
合成 profile (rtx4060-v1) 加载 ShardX 引擎:
UA一致 ✅ | HW一致 ✅ | GPU一致 ✅ | webdriver=False ✅
```

### BrowserScan 实测（`browserscan_test.py`）

```
rtx4060-v1 经 ShardX 引擎:
  隐身模式: 否 ✅
  WebGL:    无异常 ✅
  Audio:    无异常 ✅
  OS/浏览器: Windows 11 / Chrome 149 ✅
```

## 数据来源与合规

- **真机采集**: 在真实系统 Chrome 运行 probe.js 采集（本机 RTX 4070 Ti SUPER 真实基线）
- **GPU 能力表**: 170 型号的公开硬件规格（renderer 格式/设备 ID/WebGL 能力/WebGPU limits）
- **合成**: 按一致性规则从基线生成变体
- **完全自主数据**: 无第三方库导出/再分发问题（数据主权）

## 路线图

- [x] 真机采集器（本机样本）
- [x] GPU 能力表（30 型号）
- [x] 合成器 + 一致性校验
- [x] ShardX 引擎对接
- [x] 扩充 GPU 型号（30 → 170）
- [x] 多平台（macOS/Linux 合成基线）
- [x] 合成 profile 的 BrowserScan 实测
- [x] CreatorHub 集成（fp_seed 确定性选择自建库）
- [ ] 多真机采集基线扩充（更多真实样本）
- [ ] 合成 profile 的 CreepJS 实测
