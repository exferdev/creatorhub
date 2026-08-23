"""P0.2 后台管理 API: 用户管理 + 请求审计/操作审计查看。

全部 admin-only; 每次用户变更写入 RiskAdminAudit(操作审计泛化)。
选型说明: 计划中的 SQLAdmin 与本栈冲突(fastapi 0.115 钉 starlette<0.42,
sqladmin>=0.31 要求 starlette>=0.50), 故用自有最小管理面(一致性更好、零新依赖)。
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from .auth_setup import hash_password, require_roles, revoke_user_tokens
from .db import get_session
from .models import (AdminUser, RequestAudit, RiskAdminAudit)

router = APIRouter(prefix="/api/admin", tags=["admin-panel"])
ADMIN = Depends(require_roles("admin"))

_ROLES = ("admin", "operator", "viewer")


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    display_name: str = ""


class UserPatchIn(BaseModel):
    enabled: Optional[bool] = None
    role: Optional[str] = None
    display_name: Optional[str] = None


class PasswordIn(BaseModel):
    new_password: str


def _audit(action: str, actor: str, note: str = ""):
    with get_session() as s:
        s.add(RiskAdminAudit(
            action=action, actor=(actor or "admin")[:64],
            detail=json.dumps({"detail": note[:500]}, ensure_ascii=False)))
        s.commit()


def _active_admin_count(session) -> int:
    return len(session.exec(select(AdminUser).where(
        AdminUser.role == "admin", AdminUser.is_active.is_(True))).all())


def _user_out(u: AdminUser) -> dict:
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": u.role, "enabled": u.is_active, "is_superuser": u.is_superuser,
        "must_change_password": u.must_change_password,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/users")
async def list_admin_users(_admin: AdminUser = ADMIN):
    with get_session() as s:
        users = s.exec(select(AdminUser).order_by(AdminUser.id)).all()
        return {"count": len(users), "users": [_user_out(u) for u in users]}


@router.post("/users", status_code=201)
async def create_admin_user(body: UserIn, admin: AdminUser = ADMIN):
    username = body.username.strip()
    if not username or len(username) > 32:
        raise HTTPException(400, "用户名须为 1~32 字符")
    if len(body.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    role = body.role if body.role in _ROLES else "viewer"
    with get_session() as s:
        if s.exec(select(AdminUser).where(
                AdminUser.username == username)).first():
            raise HTTPException(409, "用户名已存在")
        u = AdminUser(username=username, email=f"{username}@creatorhub.local",
                      hashed_password=hash_password(body.password),
                      role=role, display_name=body.display_name.strip()[:40],
                      is_superuser=(role == "admin"))
        s.add(u)
        s.commit()
        s.refresh(u)
        out = _user_out(u)
    _audit("user.create", admin.username, f"创建用户 {username} (role={role})")
    return out


@router.patch("/users/{user_id}")
async def patch_admin_user(user_id: int, body: UserPatchIn, admin: AdminUser = ADMIN):
    with get_session() as s:
        u = s.get(AdminUser, user_id)
        if u is None:
            raise HTTPException(404, "用户不存在")
        before = _user_out(u)
        if body.enabled is not None and body.enabled != u.is_active:
            if u.id == admin.id:
                raise HTTPException(400, "不能停用自己的账号")
            if u.is_active and not body.enabled and u.role == "admin" \
                    and _active_admin_count(s) <= 1:
                raise HTTPException(400, "必须保留至少一个启用的管理员")
            u.is_active = body.enabled
        if body.role is not None and body.role != u.role:
            if body.role not in _ROLES:
                raise HTTPException(400, "角色无效")
            if u.id == admin.id and body.role != "admin":
                raise HTTPException(400, "不能降低自己的管理员角色")
            if u.role == "admin" and body.role != "admin" \
                    and _active_admin_count(s) <= 1:
                raise HTTPException(400, "必须保留至少一个管理员")
            u.role = body.role
            u.is_superuser = (body.role == "admin")
        if body.display_name is not None:
            u.display_name = body.display_name.strip()[:40]
        s.add(u)
        s.commit()
        s.refresh(u)
        out = _user_out(u)
    _audit("user.update", admin.username,
           f"更新用户 #{user_id}: {before.get('username')}")
    return out


@router.post("/users/{user_id}/password")
async def reset_user_password(user_id: int, body: PasswordIn,
                              admin: AdminUser = ADMIN):
    if len(body.new_password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    with get_session() as s:
        u = s.get(AdminUser, user_id)
        if u is None:
            raise HTTPException(404, "用户不存在")
        u.hashed_password = hash_password(body.new_password)
        u.must_change_password = False
        s.add(u)
        s.commit()
    await revoke_user_tokens(user_id)  # 旧令牌全部失效, 需重新登录
    _audit("user.password_reset", admin.username,
           f"重置用户 #{user_id} 密码")
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_admin_user(user_id: int, admin: AdminUser = ADMIN):
    with get_session() as s:
        u = s.get(AdminUser, user_id)
        if u is None:
            raise HTTPException(404, "用户不存在")
        if u.id == admin.id:
            raise HTTPException(400, "不能删除自己")
        if u.role == "admin" and u.is_active and _active_admin_count(s) <= 1:
            raise HTTPException(400, "必须保留至少一个启用的管理员")
        name = u.username
        s.delete(u)
        s.commit()
    await revoke_user_tokens(user_id)
    _audit("user.delete", admin.username, f"删除用户 #{user_id} ({name})")
    return {"ok": True}


@router.get("/users/{user_id}")
async def get_admin_user(user_id: int, _admin: AdminUser = ADMIN):
    with get_session() as s:
        u = s.get(AdminUser, user_id)
        if u is None:
            raise HTTPException(404, "用户不存在")
        return _user_out(u)


@router.get("/audit-requests")
async def list_request_audit(limit: int = 200, user_id: Optional[int] = None,
                             _admin: AdminUser = ADMIN):
    limit = max(1, min(limit, 1000))
    with get_session() as s:
        q = select(RequestAudit)
        if user_id:
            q = q.where(RequestAudit.user_id == user_id)
        rows = s.exec(q.order_by(RequestAudit.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "user_id": r.user_id, "username": r.username,
            "method": r.method, "path": r.path, "status_code": r.status_code,
            "client_ip": r.client_ip, "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


@router.get("/audit-ops")
async def list_operation_audit(limit: int = 200, _admin: AdminUser = ADMIN):
    limit = max(1, min(limit, 1000))
    with get_session() as s:
        rows = s.exec(select(RiskAdminAudit).order_by(
            RiskAdminAudit.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "action": r.action, "actor": r.actor, "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]