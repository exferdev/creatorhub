"""控制面模型: 客户端账号(username=客户端身份) / 指令队列 / 客户端推送审计。

设计: 一台 CreatorHub 客户端 = 一个 ClientAccount(username); 客户端主动注册与
轮询(内网无入口), 控制与审计都走"客户端→控制面"单方向; 登录验证集中于本服务。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ConsoleUser(SQLModel, table=True):
    """控制台用户(fastapi-users 模型 + 角色): 多个管理员(如 A、B)共用全部客户端。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str = Field(index=True, unique=True)
    hashed_password: str = ""
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)
    is_verified: bool = Field(default=True)
    role: str = Field(default="viewer", index=True)   # admin | operator | viewer
    display_name: str = ""
    must_change_password: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = None


class ConsoleAccessToken(SQLModel, table=True):
    """控制台登录令牌(fastapi-users DatabaseStrategy, 可吊销)。"""
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None


class ClientAccount(SQLModel, table=True):
    """一台 CreatorHub 客户端; username 即身份(注册时首次创建/校验密码)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = ""                    # 登录验证(本机登录也走这里)
    client_token: str = ""                     # 轮询/上报身份(注册时签发)
    note: str = ""
    disabled: bool = Field(default=False)      # Console 停用 → 客户端自锁
    registered_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: Optional[datetime] = None    # 最近一次轮询
    last_error: str = ""
    status_json: str = ""                      # 客户端上报的状态摘要(JSON)
    version: str = ""


class ClientCommand(SQLModel, table=True):
    """控制面 → 客户端的待办指令(客户端轮询取走并回执)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(index=True)
    client_name: str = ""
    op: str = Field(index=True)                # risk.set | client.disable | ...
    params: str = "{}"
    status: str = Field(default="pending", index=True)  # pending | done | failed
    result: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    done_at: Optional[datetime] = None


class ClientAudit(SQLModel, table=True):
    """客户端推送的审计(请求/操作), 供控制台远程查看。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(index=True)
    client_name: str = ""
    kind: str = Field(default="request", index=True)   # request | op
    action: str = ""
    username: str = ""
    detail: str = ""
    ok: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class ClientMetric(SQLModel, table=True):
    """数据中心趋势样本: 客户端×平台 5 分钟桶快照(保留 7 天)。

    由轮询 status.platform_stats upsert; 趋势图/矩阵来源。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(index=True)
    client_name: str = ""
    platform: str = Field(index=True)
    bucket: int = Field(index=True)        # epoch 秒 // 300
    accounts: int = Field(default=0)
    monitors: int = Field(default=0)
    works: int = Field(default=0)
    comments: int = Field(default=0)
    danmaku: int = Field(default=0)
    downloads: int = Field(default=0)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)


class AlgoKey(SQLModel, table=True):
    """算法服务客户端密钥登记(签发后需同步写入 Worker secrets 才生效)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    key_value: str = ""                    # 明文存储于控制台库(本地单机); 如需多管理员共享可后续加密
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AlgoMetricSample(SQLModel, table=True):
    """算法服务遥测历史样本(Console 侧累计, 供图表; 保留最近 7 天)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    payload_json: str = ""                 # /metrics 快照 JSON


class ConsoleAudit(SQLModel, table=True):
    """控制面自身操作审计: 哪位管理员对哪台客户端做了什么。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    username: str = ""
    client_id: Optional[int] = Field(default=None, index=True)
    client_name: str = ""
    action: str = Field(index=True)
    ok: bool = Field(default=True)
    detail: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)