"""控制面(独立管理后台)数据模型: 控制台用户 / 实例注册 / 操作审计。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ConsoleUser(SQLModel, table=True):
    """控制台用户(fastapi-users 模型 + 角色)。"""
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


class ConsoleInstance(SQLModel, table=True):
    """注册的 CreatorHub 实例。

    凭据策略: 注册/重新授权时用实例 admin 账号密码换取 token, **密码不落盘**;
    token 有过期时间, 过期后标记凭据失效并提示重新授权。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str = Field(index=True, unique=True)
    token: str = ""
    token_expires_at: Optional[datetime] = None
    admin_username: str = ""          # 仅展示用
    note: str = ""
    enabled: bool = Field(default=True)
    last_ok_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConsoleAudit(SQLModel, table=True):
    """控制面操作审计: 谁在何时对哪台实例做了什么。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    username: str = ""
    instance_id: Optional[int] = Field(default=None, index=True)
    instance_name: str = ""
    action: str = Field(index=True)   # instance.add / user.create / risk.put / ...
    ok: bool = Field(default=True)
    detail: str = ""                  # 短备注, 不含凭据
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)