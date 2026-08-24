"""Console 后台管理界面(starlette-admin, Django Admin 式)。

服务端渲染管理台: 客户端账号/审计/指令/用户 数据浏览·筛选·编辑由 ModelView
自动生成; 启停/重置密码/下发指令等动作走现有 /api/admin/clients/*(原前端页保留
为动作入口)。需要独立依赖栈(requirements-console.txt): fastapi(新版)+starlette-admin。
"""
from __future__ import annotations

import os
import secrets
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette_admin import BaseAdmin
from starlette_admin.auth import AdminUser, AuthProvider
from starlette_admin.contrib.sqla import ModelView
from starlette_admin.exceptions import LoginFailed

from .db import get_session
from .models import ClientAccount, ClientAudit, ClientCommand, ConsoleUser

ADMIN_SECRET = os.environ.get("CONSOLE_ADMIN_SECRET") or secrets.token_hex(16)


class ConsoleAuthProvider(AuthProvider):
    """后台登录: 校验 Console 用户(admin/operator 可进), 会话保持。"""

    async def login(self, username: str, password: str, remember_me: bool,
                    request: Request, response: Any) -> Any:
        from fastapi_users.password import PasswordHelper
        from sqlmodel import select
        ph = PasswordHelper()
        with get_session() as s:
            u = s.exec(select(ConsoleUser).where(
                ConsoleUser.username == username)).first()
            if u is None or not u.is_active or u.role not in ("admin", "operator"):
                raise LoginFailed("用户名或密码错误")
            ok, _ = ph.verify_and_update(password, u.hashed_password)
            if not ok:
                raise LoginFailed("用户名或密码错误")
            role = u.role
        if remember_me:
            response.set_cookie("remember_me", "yes", max_age=30 * 86400)
        request.session.update({"console_user": username, "role": role})
        return response

    async def is_authenticated(self, request: Request) -> bool:
        return bool(request.session.get("console_user"))

    def get_admin_user(self, request: Request) -> Optional[AdminUser]:
        # 注意: starlette-admin 0.17 为同步接口
        name = request.session.get("console_user")
        if not name:
            return None
        return AdminUser(username=name)

    async def logout(self, request: Request, response: Any) -> Any:
        request.session.clear()
        return RedirectResponse("/admin")


class ClientAccountView(ModelView):
    """客户端账号: 列表/筛选/编辑; 敏感字段不展示不编辑。"""
    name = "客户端账号"
    label = "客户端账号"
    identity = "client"
    fields = ["id", "username", "note", "disabled", "version",
              "registered_at", "last_seen_at", "last_error", "status_json"]
    exclude_fields_from_list = ["status_json"]
    exclude_fields_from_edit = ["id", "username", "version", "registered_at",
                                "last_seen_at", "last_error", "status_json",
                                "password_hash", "client_token"]
    exclude_fields_from_create = list(fields)
    searchable_fields = ["username", "note"]
    sortable_fields = ["id", "username", "disabled", "last_seen_at"]
    page_size = 25


class ClientAuditView(ModelView):
    """客户端推送的审计(只读)。"""
    name = "客户端审计"
    label = "客户端审计"
    identity = "clientaudit"
    fields = ["id", "client_name", "kind", "action", "username",
              "detail", "ok", "created_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    exclude_fields_from_detail = ["id"]
    searchable_fields = ["client_name", "action", "username"]
    sortable_fields = ["id", "client_name", "created_at"]
    page_size = 50


class ClientCommandView(ModelView):
    """指令队列与回执(只读)。"""
    name = "指令记录"
    label = "指令记录"
    identity = "clientcmd"
    fields = ["id", "client_name", "op", "params", "status", "result",
              "created_at", "done_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    searchable_fields = ["client_name", "op", "status"]
    sortable_fields = ["id", "client_name", "status", "created_at"]
    page_size = 50


class ConsoleUserView(ModelView):
    """控制台用户(只读浏览; 建号/改密走现有 /api/admin/users)。"""
    name = "控制台用户"
    label = "控制台用户"
    identity = "consoleuser"
    fields = ["id", "username", "display_name", "role", "is_active",
              "is_superuser", "created_at", "last_login_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    searchable_fields = ["username", "display_name", "role"]
    sortable_fields = ["id", "username", "role", "created_at"]
    page_size = 25


def build_admin(engine) -> BaseAdmin:
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    from starlette_admin.contrib.sqla import Admin as SqlaAdmin
    # 注意: 必须用 sqla 的 Admin(自动挂 SQLAlchemyMiddleware → request.state.session)
    admin = SqlaAdmin(
        engine,
        title="CreatorHub Console 后台",
        base_url="/admin",
        auth_provider=ConsoleAuthProvider(),
        middlewares=[Middleware(SessionMiddleware, secret_key=ADMIN_SECRET)],
    )
    admin.add_view(ClientAccountView(ClientAccount))
    admin.add_view(ClientAuditView(ClientAudit))
    admin.add_view(ClientCommandView(ClientCommand))
    admin.add_view(ConsoleUserView(ConsoleUser))
    return admin