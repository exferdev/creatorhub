"""控制面鉴权: fastapi-users 同步适配器 + 登录限流 + 首启管理员。

模式与 CreatorHub app/auth_setup.py 一致(同步 session 适配), 独立实例化。
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Optional

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, BearerTransport
from fastapi_users.authentication.strategy import DatabaseStrategy
from fastapi_users.authentication.strategy.db.adapter import AccessTokenDatabase
from fastapi_users.db.base import BaseUserDatabase
from fastapi_users.exceptions import InvalidID
from fastapi_users.password import PasswordHelper
from sqlmodel import select

from .db import get_session
from .models import ConsoleAccessToken, ConsoleUser

log = logging.getLogger("creatorhub.console.auth")
ID = int

# ── 登录限流(防爆破): 每用户名滑动窗口, 成功清零 ──
_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SECONDS = 60.0
_login_windows: dict = {}


def _login_window_gate(username: str):
    import time as _t
    now = _t.time()
    win = _login_windows.setdefault(username, [])
    while win and now - win[0] > _LOGIN_WINDOW_SECONDS:
        win.pop(0)
    if len(win) >= _LOGIN_LIMIT:
        raise HTTPException(429, "登录尝试过于频繁, 请稍后再试")
    win.append(now)


def _login_window_clear(username: str):
    _login_windows.pop(username, None)


class SyncUserDatabase(BaseUserDatabase[ConsoleUser, ID]):
    def __init__(self, session):
        self.session = session

    async def create(self, user: ConsoleUser) -> ConsoleUser:
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def delete(self, user: ConsoleUser) -> None:
        self.session.delete(user)
        self.session.commit()

    async def get(self, user_id: ID) -> Optional[ConsoleUser]:
        return self.session.get(ConsoleUser, user_id)

    async def get_by_email(self, email: str) -> Optional[ConsoleUser]:
        return self.session.exec(
            select(ConsoleUser).where(
                (ConsoleUser.username == email) | (ConsoleUser.email == email))
        ).first()

    async def get_by_oauth_account(self, oauth: str, account_id: str) -> None:
        return None

    async def update(self, user: ConsoleUser, update_dict: dict) -> ConsoleUser:
        for key, value in update_dict.items():
            if hasattr(user, key):
                setattr(user, key, value)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    async def add_oauth_account(self, user, oauth_account) -> ConsoleUser:
        return user

    async def update_oauth_account(self, user, oauth_account) -> ConsoleUser:
        return user


class SyncAccessTokenDatabase(AccessTokenDatabase[ConsoleAccessToken]):
    def __init__(self, session):
        self.session = session

    async def create(self, create_dict: dict) -> ConsoleAccessToken:
        token = ConsoleAccessToken(**create_dict)
        self.session.add(token)
        self.session.commit()
        self.session.refresh(token)
        return token

    async def get_by_token(self, token: str, max_age=None) -> Optional[ConsoleAccessToken]:
        row = self.session.get(ConsoleAccessToken, token)
        if row is None:
            return None
        if max_age is not None and row.created_at.replace(tzinfo=timezone.utc) < max_age:
            return None
        return row

    async def update(self, token, update_dict: dict) -> ConsoleAccessToken:
        for key, value in update_dict.items():
            if hasattr(token, key):
                setattr(token, key, value)
        self.session.add(token)
        self.session.commit()
        return token

    async def delete(self, token) -> None:
        self.session.delete(token)
        self.session.commit()

    async def delete_all_for_user(self, user_id: ID) -> int:
        rows = list(self.session.exec(
            select(ConsoleAccessToken).where(
                ConsoleAccessToken.user_id == user_id)).all())
        for row in rows:
            self.session.delete(row)
        self.session.commit()
        return len(rows)


def get_user_db() -> AsyncGenerator[SyncUserDatabase, None]:
    with get_session() as s:
        yield SyncUserDatabase(s)


def get_token_db() -> AsyncGenerator[SyncAccessTokenDatabase, None]:
    with get_session() as s:
        yield SyncAccessTokenDatabase(s)


async def get_user_manager(
        user_db: SyncUserDatabase = Depends(get_user_db)
) -> AsyncGenerator[ConsoleUserManager, None]:
    yield ConsoleUserManager(user_db)


class ConsoleUserManager(BaseUserManager[ConsoleUser, ID]):
    def parse_id(self, value: Any) -> ID:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise InvalidID() from None

    async def authenticate(self, credentials) -> Optional[ConsoleUser]:
        name = str(getattr(credentials, "username", "") or "")
        _login_window_gate(name)
        user = await super().authenticate(credentials)
        if user is not None:
            _login_window_clear(name)
        return user  # None 由路由映射为 LOGIN_BAD_CREDENTIALS

    async def on_after_login(self, user, request=None, response=None):
        try:
            self.user_db.session.add(user)
            user.last_login_at = datetime.utcnow()
            self.user_db.session.commit()
        except Exception as e:
            log.warning("last_login 记录失败: %r", e)


def get_strategy(token_db: SyncAccessTokenDatabase = Depends(get_token_db),
                 ) -> DatabaseStrategy:
    lifetime = 14 * 86400
    return DatabaseStrategy(token_db, lifetime_seconds=lifetime)


auth_backend = AuthenticationBackend(
    name="db-bearer",
    transport=BearerTransport(tokenUrl="/api/console/auth/login"),
    get_strategy=get_strategy,
)

fastapi_users = FastAPIUsers[ConsoleUser, ID](get_user_manager, [auth_backend])

current_user = fastapi_users.current_user(active=True)


def require_roles(*roles: str):
    """RBAC: 角色白名单; admin/超管恒通过; 测试旁路视为超管。"""
    async def require_current_user(request: Request) -> ConsoleUser:
        u = getattr(request.state, "user", None)
        if u is not None:
            return u
        if auth_bypass_enabled():
            return ConsoleUser(id=0, username="__bypass__", role="admin",
                               is_superuser=True, is_active=True)
        raise HTTPException(401, "未登录或令牌无效")

    async def dep(user: ConsoleUser = Depends(require_current_user)) -> ConsoleUser:
        if user.role not in roles and not user.is_superuser:
            raise HTTPException(403, "权限不足")
        return user
    return dep


def auth_bypass_enabled() -> bool:
    """测试旁路: 仅 CREATORHUB_TEST_AUTH_BYPASS=1(整仓测试约定)。"""
    return os.environ.get("CREATORHUB_TEST_AUTH_BYPASS", "").strip() == "1"


def ensure_bootstrap_console_admin(db_path: str) -> bool:
    """首启创建控制台管理员(随机密码, 打印到日志/控制台)。"""
    from pathlib import Path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    from .db import init_db as _init_db
    _init_db(db_path)
    with get_session() as s:
        if s.exec(select(ConsoleUser)).first() is not None:
            return False
        env_pw = os.environ.get("CREATORHUB_CONSOLE_ADMIN_PASSWORD", "").strip()
        password = env_pw or secrets.token_urlsafe(12)
        user = ConsoleUser(
            username="admin", email="admin@console.local",
            hashed_password=PasswordHelper().hash(password),
            is_active=True, is_superuser=True, is_verified=True,
            role="admin", must_change_password=not bool(env_pw))
        s.add(user)
        s.commit()
        print(f"[console] 已创建控制台管理员 admin"
              f"{' (初始密码见环境变量)' if env_pw else f', 初始密码: {password}'}"
              f" —— 请尽快修改")
        return True


def hash_password(plain: str) -> str:
    return PasswordHelper().hash(plain)