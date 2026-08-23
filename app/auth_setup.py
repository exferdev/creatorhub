"""fastapi-users 同步 DB 适配器 + 后台鉴权装配(用户管理/多用户前置)。

项目全链路用同步 SQLAlchemy/SQLModel session; fastapi-users 官方适配器面向
AsyncSession。这里实现最小同步适配器(BaseUserDatabase / AccessTokenDatabase),
把 fastapi-users 的异步回调映射到同步 session —— 社区常见做法, 不为鉴权引入
第二套异步 ORM 栈, 也符合"不重复造轮子"(协议层复用 fastapi-users, 只写接线)。

鉴权语义(P0.0):
  - 所有 /api/* 请求必须携带有效令牌(本机同样要登录)。
    白名单: / , /health, /docs*, /openapi.json, /api/admin/auth/*(登录接口本身)。
  - 令牌: DatabaseStrategy 落库(Bearer, 可吊销); 有效期内随时可删令牌踢人。
  - 角色: admin(全可见+用户管理) / operator(日常操作) / viewer(只读), 见 require_roles。
  - 首启无用户时自动创建 admin(随机密码打印到日志/控制台, 见 ensure_bootstrap_admin)。
  - CREATORHUB_TEST_AUTH_BYPASS=1 时全局跳过鉴权(仅测试环境使用)。

P0.1 起业务数据按 owner_id 隔离; 权限助手见 main.py 的 _owned。
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Optional

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users.exceptions import InvalidID
from fastapi_users.authentication import AuthenticationBackend, BearerTransport
from fastapi_users.authentication.strategy import DatabaseStrategy
from fastapi_users.authentication.strategy.db.adapter import AccessTokenDatabase
from fastapi_users.db.base import BaseUserDatabase
from fastapi_users.password import PasswordHelper
from sqlmodel import select

from .config import Config
from .db import get_session
from .models import AdminAccessToken, AdminUser

log = logging.getLogger("creatorhub.auth")
ID = int

# ── 白名单: 无需登录的路径 ──
AUTH_OPEN_EXACT = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
AUTH_OPEN_PREFIXES = ("/api/admin/auth/",)


def auth_bypass_enabled() -> bool:
    """测试旁路: 仅 CREATORHUB_TEST_AUTH_BYPASS=1 时生效(生产不设置)。"""
    return os.environ.get("CREATORHUB_TEST_AUTH_BYPASS", "").strip() == "1"


# ── 同步数据库适配器 ──
class SyncUserDatabase(BaseUserDatabase[AdminUser, ID]):
    """BaseUserDatabase → 我们的同步 session。"""

    def __init__(self, session):
        self.session = session

    async def create(self, user: AdminUser) -> AdminUser:
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def delete(self, user: AdminUser) -> None:
        self.session.delete(user)
        self.session.commit()

    async def get(self, user_id: ID) -> Optional[AdminUser]:
        return self.session.get(AdminUser, user_id)

    async def get_by_email(self, email: str) -> Optional[AdminUser]:
        # v15 username 登录: credentials.username 会以 email 参数传入, 按 username/email 双查
        return self.session.exec(
            select(AdminUser).where(
                (AdminUser.username == email) | (AdminUser.email == email))
        ).first()

    async def get_by_oauth_account(self, oauth: str, account_id: str) -> None:
        return None  # 本项目不用 OAuth

    async def update(self, user: AdminUser, update_dict: dict[str, Any]) -> AdminUser:
        for key, value in update_dict.items():
            if hasattr(user, key):
                setattr(user, key, value)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def add_oauth_account(self, user: AdminUser, oauth_account) -> AdminUser:
        return user

    async def update_oauth_account(self, user: AdminUser, oauth_account) -> AdminUser:
        return user


class SyncAccessTokenDatabase(AccessTokenDatabase[AdminAccessToken]):
    """AccessTokenDatabase → 同步 session (DatabaseStrategy 落库令牌)。"""

    def __init__(self, session):
        self.session = session

    async def create(self, create_dict: dict[str, Any]) -> AdminAccessToken:
        token = AdminAccessToken(**create_dict)
        self.session.add(token)
        self.session.commit()
        self.session.refresh(token)
        return token

    async def get_by_token(self, token: str, max_age: Optional[datetime] = None
                           ) -> Optional[AdminAccessToken]:
        row = self.session.get(AdminAccessToken, token)
        if row is None:
            return None
        if max_age is not None and row.created_at.replace(tzinfo=timezone.utc) < max_age:
            return None  # 过期
        return row

    async def update(self, token: AdminAccessToken, update_dict: dict[str, Any]) -> AdminAccessToken:
        for key, value in update_dict.items():
            if hasattr(token, key):
                setattr(token, key, value)
        self.session.add(token)
        self.session.commit()
        return token

    async def delete(self, token: AdminAccessToken) -> None:
        self.session.delete(token)
        self.session.commit()

    async def delete_all_for_user(self, user_id: ID) -> int:
        rows = list(self.session.exec(
            select(AdminAccessToken).where(AdminAccessToken.user_id == user_id)).all())
        for row in rows:
            self.session.delete(row)
        self.session.commit()
        return len(rows)


# ── 依赖: session -> adapter -> manager ──
def get_user_db() -> AsyncGenerator[SyncUserDatabase, None]:
    with get_session() as s:
        yield SyncUserDatabase(s)


def get_token_db() -> AsyncGenerator[SyncAccessTokenDatabase, None]:
    with get_session() as s:
        yield SyncAccessTokenDatabase(s)


async def get_user_manager(
        user_db: SyncUserDatabase = Depends(get_user_db)
) -> AsyncGenerator[AdminUserManager, None]:
    yield AdminUserManager(user_db)


class AdminUserManager(BaseUserManager[AdminUser, ID]):
    """校验用 UserManager: 默认密码强度足够本地场景, 登录后记录 last_login_at。"""

    def parse_id(self, value: Any) -> ID:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise InvalidID() from None

    async def on_after_login(self, user: AdminUser, request: Optional[Request] = None,
                             response=None):
        try:
            self.user_db.session.add(user)
            user.last_login_at = datetime.utcnow()
            self.user_db.session.commit()
        except Exception as e:  # 登录成功但记录失败不阻断
            log.warning("last_login 记录失败: %r", e)


def get_strategy(
        token_db: SyncAccessTokenDatabase = Depends(get_token_db),
) -> DatabaseStrategy:
    from .config import load_config
    cfg = load_config()
    lifetime = max(1, int(cfg.admin.token_days)) * 86400
    return DatabaseStrategy(token_db, lifetime_seconds=lifetime)


auth_backend = AuthenticationBackend(
    name="db-bearer",
    transport=BearerTransport(tokenUrl="/api/admin/auth/login"),
    get_strategy=get_strategy,
)

fastapi_users = FastAPIUsers[AdminUser, ID](get_user_manager, [auth_backend])

current_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)


def require_roles(*roles: str):
    """RBAC 依赖: 角色白名单; admin 恒通过; 测试旁路视为超管。"""
    async def require_current_user(request: Request) -> AdminUser:
        u = getattr(request.state, "user", None)
        if u is not None:
            return u
        if auth_bypass_enabled():
            return AdminUser(id=0, username="__bypass__", role="admin",
                             is_superuser=True, is_active=True)
        raise HTTPException(401, "未登录或令牌无效")

    async def dep(user: AdminUser = Depends(require_current_user)) -> AdminUser:
        if user.role not in roles and not user.is_superuser:
            raise HTTPException(403, "权限不足")
        return user
    return dep


# ── 首启管理员 ──
def ensure_bootstrap_admin(cfg: Config) -> bool:
    """用户表为空时创建管理员。密码: env CREATORHUB_ADMIN_INITIAL_PASSWORD 或随机。
    返回是否新建。初始密码打印到日志/控制台(desktop.log 可见), 需尽快修改。"""
    from fastapi_users.password import PasswordHelper
    with get_session() as s:
        if s.exec(select(AdminUser)).first() is not None:
            return False
        env_pw = os.environ.get("CREATORHUB_ADMIN_INITIAL_PASSWORD", "").strip()
        password = env_pw or secrets.token_urlsafe(12)
        ph = PasswordHelper()
        user = AdminUser(
            username="admin",
            email="admin@creatorhub.local",
            hashed_password=ph.hash(password),
            is_active=True,
            is_superuser=True,
            is_verified=True,
            role="admin",
            must_change_password=not bool(env_pw),
        )
        s.add(user)
        s.commit()
        print(f"[auth] 已创建管理员账号 admin"
              f"{' (初始密码见环境变量)' if env_pw else f', 初始密码: {password}'}"
              f" —— 首次登录后请尽快修改密码")
        return True


# ── 令牌解析(中间件用) ──
def token_from_request(request: Request) -> str:
    """从 Authorization: Bearer 或 ?access_token= 取令牌(后者给 <img>/<video> 等
    无法自带头部的静态化 GET 使用)。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("access_token") or "").strip()


async def user_from_token(token: str, cfg: Config) -> Optional[AdminUser]:
    """令牌 → 用户(校验存在/有效/未过期/账号启用)。"""
    if not token:
        return None
    with get_session() as s:
        row = s.get(AdminAccessToken, token)
        if row is None:
            return None
        if cfg.admin.token_days > 0:
            age = datetime.utcnow() - row.created_at
            if age.total_seconds() > cfg.admin.token_days * 86400:
                return None
        user = s.get(AdminUser, row.user_id)
        if user is None or not user.is_active:
            return None
        return user


async def revoke_user_tokens(user_id: ID) -> int:
    """吊销某用户全部令牌(改密/封号/踢人用)。"""
    with get_session() as s:
        return await SyncAccessTokenDatabase(s).delete_all_for_user(user_id)


def hash_password(plain: str) -> str:
    return PasswordHelper().hash(plain)