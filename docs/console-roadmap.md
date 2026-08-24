# CreatorHub Console 中控台规划（Roadmap）

> 目标：Console 从「客户端管理台」升级为「CreatorHub 舰队运营中控台」。
> 原则：先锚定 GitHub 成熟方案，不重复造轮子；算法本体在 exferdev/js（签名服务），Console 是其运营入口。

## 侧边栏目标结构（15 项，四组）

```
中控台       仪表盘 · 数据中心 · 远程任务
资源管理     客户端账号 · 指纹浏览器 · 算法中心
系统         用户管理 · 操作审计 · 接入指引 · 修改密码 · 设置
```

---

## ① 中控台 / 仪表盘（现有 ← 升级）

| 内容 | 数据来源 | 依赖 |
|---|---|---|
| 全局矩阵：在线 / 任务执行中 / 风险告警 / 今日发布量 | 客户端轮询上报扩展 | — |
| 跨平台账号·作品·发布趋势图 | `ClientMetrics` 每日快照表（新） | **Apache ECharts** (61k★) |
| 告警流（离线 / 任务失败 / 风控事件） | Console 事件表（新） | 见 ⑦ Apprise |

轮子锚点：ECharts（趋势/分布/大屏一站解决）；sqlite 快照够用（几十客户端×每日），不引时序库。

## ② 数据中心（平台维度，新增）

- 页面：平台总览（抖音/小红书/快手/视频号 × 账号数 / 作品数 / 发布数 / 监控目标 / 评论 / 弹幕采集量）→ 下钻客户端 → 导出 CSV/Excel。
- 数据链路：客户端轮询上报扩展 `platform_stats`（本地库计数）→ Console 落 `ClientMetrics` 快照 → 聚合展示。
- 轮子锚点：Metabase/Superset 过重不引；ECharts + sqlite 聚合薄层。

## ③ 远程任务（现有指令队列 ← 升级为通用任务系统）

| 能力 | 现状 | 规划 |
|---|---|---|
| 下发 | 仅 `risk.set` | op 白名单扩展：`monitor.run_now` / `collection.run` / `publish.run` / `comment.collect` / `danmaku.collect` / `profile.check` / `fingerprint.set` |
| 执行 | 单一处理函数 | 客户端改**执行器注册表**（封装现有 engine 动作，不新造执行逻辑） |
| 跟踪 | 状态 + 回执 | 任务历史页（筛选/重试=重排队/结果 JSON 详情） |

轮子锚点：即时任务用现有轮询队列；仅当需要定时/周期任务时引 **APScheduler** (10k★)；**不引 Celery**（队列就在控制面本地）。

## ④ 指纹浏览器（新模块，v1/v2）

- v1（管理/维护）：指纹汇总页 —— 每客户端 Profile 数、浏览器引擎 / Chrome 版本矩阵（Patchright 底座支持的 Chrome/Chromium 基线，版本不符标红）、Profile 健康状态、指纹方案版本；`profile.check` 自检指令；客户端**只上报摘要**（不含 Cookie 等敏感数据）。
- v2（策略管理）：`fingerprint.set` 统一下发指纹方案模板（防关联参数集：UA/Canvas/WebGL/时区/语言/屏幕…）→ 客户端应用 + 回执。
- 轮子锚点：指纹运行时**不引新库**（客户端已有自研指纹+签名体系；playwright-stealth / fingerprint-suite 仅作参照）；Console 侧只做摘要/矩阵/模板管理（starlette-admin 模式）。

## ⑤ 算法中心（= exferdev/js 签名服务的升级 + Console 运营入口）

**本体**：`github.com/exferdev/js` —— Cloudflare Worker，自定义域 js.faryi.com，
`POST /sign/:platform/:algorithm` + 指纹 API；客户端 `app/platforms/*/sign_client.py` 调用（带重试/超时/last_error 统计）。

### 升级方向（全用 Cloudflare 官方能力，不造轮子）

| 能力 | js 项目（Worker 侧） | Console 侧 |
|---|---|---|
| **算法版本管理** | 算法实现不可变版本：`/sign/:platform/:algorithm/:version`；版本注册表 + 默认版本指针（可切换/回滚） | 算法注册表页（平台×算法×版本×默认指针），切换调 Worker 管理端点 |
| **鉴权** | 客户端 API Key（`X-Api-Key`，HMAC 校验） | 密钥签发/吊销（Console 存 + 下发客户端配置） |
| **遥测** | `/metrics`（公网带 key JSON：QPS/错误率/p95/各算法计数，60s 窗口） | 定期拉取 → ECharts（QPS/错误率/延迟趋势） |
| **健康自检** | `/health`：用已知输入向量断言各算法输出签名正确 | 多客户端健康面板（两级：服务健康 + 客户端命中健康） |
| **指纹 API 管理** | 指纹模板版本 + 生成统计 | 模板版本查看、生成量统计 |
| **灰度/回滚** | Cloudflare **Version Pinning / Rolling Deployments**（官方发布能力） | 只展示发布状态 |

### 客户端侧改造

- `sign_client.py`：加 `X-Api-Key`；窗口统计（成功率/失败原因/耗时 p95）→ `sign_health` 随轮询上报。
- 算法参数（间隔/核验模式等行为参数）仍走现有 `risk.set`/策略指令，**与算法服务解耦**。

## ⑥ 用户管理（两层次）

| 层次 | 现状 | 规划 |
|---|---|---|
| 控制台用户（A、B…多管理员） | fastapi-users 登录/RBAC/限流已落地；后台只读视图 | 补建号/角色/启停/重置密码表单页（Django Admin UserAdmin 模式，复用 `/api/admin/users*`） |
| 客户端账号（一台客户端=一账号） | 列表 + 行内动作 | 批量操作（勾选多台统一启停） |

轮子锚点：fastapi-users（已用）+ starlette-admin（已用）。

## ⑦ 设置 / 告警（新）

| 设置项 | 实现 |
|---|---|
| 管理告警 | **Apprise** (10k★)：一处配置 邮件/Telegram/钉钉/Webhook；触发：客户端离线、任务失败、风控事件、签名服务错误率超阈 |
| 审计保留策略 | >90 天审计/指标定时清理（asyncio 后台任务） |
| 会话/轮询默认值 | KV 设置表 + 展示 |

---

## 客户端依赖缺口汇总

1. `_collect_status` 扩展：`platform_stats` / Profile 摘要 / `engine_info`（引擎+Chrome 版本）/ `sign_health`（M1/M3）
2. `_execute_command` → 执行器注册表（M2）
3. Profile 自检函数 `profile.check`（M3）
4. `sign_client.py`：API Key + 窗口统计（M4-js 联动）

## 里程碑（建议顺序）

| Milestone | 内容 | 主要页面 |
|---|---|---|
| **M1 数据底座** | 上报协议扩展 + `ClientMetrics` + ECharts | 数据中心、中控台升级 |
| **M2 远程任务** | 执行器注册表 + op 白名单 + 任务历史/重试 | 远程任务 |
| **M3 指纹中心** | 摘要上报 + 版本矩阵 + `profile.check` | 指纹浏览器 v1 |
| **M4 算法中心** | exferdev/js 升级（版本/密钥/遥测/健康）+ Console 算法页 + `sign_health` 上报 | 算法中心 |
| **M5 设置/告警** | Apprise + 保留策略 + KV | 设置页 |
| **M6 用户管理页** | ConsoleUser CRUD 表单 + 批量操作 | 用户管理 |

> 依赖说明：M4 需要 `exferdev/js` 仓库接入（独立仓库/独立部署，与 Console 走公网 HTTPS 通信）。