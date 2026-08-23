"""控制面(独立网页管理后台)主服务。

独立部署:  python -m uvicorn console.main:app --host 127.0.0.1 --port 8100
(异地访问请自行反代 TLS)。数据: console/data/console.db(独立于 CreatorHub)。
功能: 实例注册/健康/凭据续期 + 远端用户管理 + 远端风控查看调整 + 审计查看;
      控制面自身用户(admin/operator/viewer)登录, 每次操作落 ConsoleAudit。
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import select

from .client import InstanceClient, InstanceError
from .console_auth import (auth_backend, auth_bypass_enabled, current_user,
                           ensure_bootstrap_console_admin, fastapi_users,
                           hash_password, require_roles)
from .db import get_session
from .models import ConsoleAccessToken, ConsoleAudit, ConsoleInstance, ConsoleUser

CONSOLE_DB = os.environ.get(
    "CONSOLE_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "console" / "data" / "console.db"))
WEB_DIR = Path(__file__).resolve().parent / "web"

_admin_only = require_roles("admin")
_any_user = require_roles("admin", "operator", "viewer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if ensure_bootstrap_console_admin(CONSOLE_DB):
            print("[console] 控制台管理员已初始化(admin), 初始密码见上")
    except Exception as e:
        print(f"[console] 初始化失败(不影响启动): {e!r}")
    yield


app = FastAPI(title="CreatorHub Console", lifespan=lifespan)
app.include_router(fastapi_users.get_auth_router(auth_backend),
                   prefix="/api/console/auth")


class MeOut(BaseModel):
    id: int
    username: str
    display_name: str = ""
    role: str
    is_superuser: bool = False


@app.get("/api/console/me", response_model=MeOut)
async def console_me(user: ConsoleUser = Depends(current_user)):
    return MeOut(id=user.id, username=user.username,
                 display_name=user.display_name, role=user.role,
                 is_superuser=user.is_superuser)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/console/me/password")
async def console_change_password(body: ChangePasswordIn,
                                  user: ConsoleUser = Depends(current_user)):
    from fastapi_users.password import PasswordHelper
    ph = PasswordHelper()
    if not ph.verify_and_update(body.current_password, user.hashed_password)[0]:
        raise HTTPException(400, "当前密码不正确")
    if len(body.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    user.hashed_password = ph.hash(body.new_password)
    user.must_change_password = False
    with get_session() as s:
        s.merge(user)
        s.commit()
    from .console_auth import SyncAccessTokenDatabase
    with get_session() as s:
        await SyncAccessTokenDatabase(s).delete_all_for_user(user.id)
    return {"ok": True}


# ── 全局守卫: 除白名单外必须携带控制台令牌 ──
OPEN_EXACT = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
OPEN_PREFIX = ("/static", "/api/console/auth/")


async def _resolve_console_user(token: str):
    if not token:
        return None
    with get_session() as s:
        row = s.get(ConsoleAccessToken, token)
        if row is None:
            return None
        # 有效期内(14 天)
        if datetime.utcnow() - row.created_at > timedelta(days=14):
            return None
        return s.get(ConsoleUser, row.user_id)


@app.middleware("http")
async def console_guard(request: Request, call_next):
    if auth_bypass_enabled():
        return await call_next(request)
    path = request.url.path
    if path in OPEN_EXACT or path.startswith(OPEN_PREFIX):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user = await _resolve_console_user(token)
    if user is None or not user.is_active:
        return JSONResponse({"detail": "未登录或令牌无效"}, status_code=401)
    request.state.user = user
    return await call_next(request)


# ── 审计助手 ──
def _audit(request: Request, instance: ConsoleInstance | None, action: str,
           ok: bool = True, detail: str = ""):
    u = getattr(request.state, "user", None)
    try:
        with get_session() as s:
            s.add(ConsoleAudit(
                user_id=getattr(u, "id", None),
                username=getattr(u, "username", "") or "",
                instance_id=instance.id if instance else None,
                instance_name=instance.name if instance else "",
                action=action, ok=ok, detail=detail[:500]))
            s.commit()
    except Exception:
        pass


def _instance_or_404(session, instance_id: int) -> ConsoleInstance:
    inst = session.get(ConsoleInstance, instance_id)
    if inst is None:
        raise HTTPException(404, "实例不存在")
    return inst


def _client_for(inst: ConsoleInstance) -> InstanceClient:
    return InstanceClient(inst.base_url, inst.token)


def _require_valid_token(request: Request, inst: ConsoleInstance):
    """令牌过期则先给"重新授权"信号, 不发出请求。"""
    if not inst.token or InstanceClient.token_expired(inst.token_expires_at):
        _audit(request, inst, "instance.token_expired", ok=False,
               detail="凭据已过期, 需重新授权")
        raise HTTPException(401, "实例凭据已过期, 请重新授权")
    return _client_for(inst)


def _map_instance_error(request: Request, inst: ConsoleInstance,
                        e: InstanceError) -> HTTPException:
    with get_session() as s:
        inst2 = s.get(ConsoleInstance, inst.id)
        if inst2:
            inst2.last_error = str(e)[:300]
            s.add(inst2)
            s.commit()
    _audit(request, inst, "instance.error", ok=False, detail=str(e)[:300])
    if e.kind == "auth_expired":
        return HTTPException(401, str(e))
    if e.kind == "offline":
        return HTTPException(502, str(e))
    return HTTPException(502, str(e))


# ── 实例注册 / 管理(admin)──
class InstanceIn(BaseModel):
    name: str
    base_url: str
    admin_username: str = "admin"
    admin_password: str = ""
    note: str = ""


class ReauthIn(BaseModel):
    username: str = "admin"
    password: str


@app.get("/api/instances")
async def list_instances(request: Request,
                         _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        insts = s.exec(select(ConsoleInstance).order_by(ConsoleInstance.id)).all()
        out = []
        for inst in insts:
            client = InstanceClient(inst.base_url, inst.token)
            online = await client.health()
            out.append({
                "id": inst.id, "name": inst.name, "base_url": inst.base_url,
                "admin_username": inst.admin_username, "note": inst.note,
                "enabled": inst.enabled, "online": online,
                "token_ok": bool(inst.token)
                and not InstanceClient.token_expired(inst.token_expires_at),
                "last_error": inst.last_error,
                "last_ok_at": inst.last_ok_at.isoformat() if inst.last_ok_at else None,
            })
        _audit(request, None, "instance.list")
        return {"count": len(out), "instances": out}


@app.post("/api/instances", status_code=201)
async def add_instance(request: Request, body: InstanceIn,
                       _admin: ConsoleUser = Depends(_admin_only)):
    name = body.name.strip()
    base = body.base_url.strip().rstrip("/")
    if not name or not base:
        raise HTTPException(400, "名称与地址必填")
    if not body.admin_password:
        raise HTTPException(400, "请输入实例 admin 密码以换取令牌(密码不落盘)")
    client = InstanceClient(base)
    try:
        token = await client.auth_login(body.admin_username.strip() or "admin",
                                        body.admin_password)
    except InstanceError as e:
        raise HTTPException(400, f"实例登录失败: {e}")
    with get_session() as s:
        if s.exec(select(ConsoleInstance).where(
                ConsoleInstance.name == name)).first():
            raise HTTPException(409, "实例名称已存在")
        if s.exec(select(ConsoleInstance).where(
                ConsoleInstance.base_url == base)).first():
            raise HTTPException(409, "实例地址已存在")
        inst = ConsoleInstance(
            name=name, base_url=base, admin_username=body.admin_username.strip(),
            token=token, token_expires_at=datetime.utcnow() + timedelta(days=14),
            note=body.note.strip()[:200])
        s.add(inst)
        s.commit()
        s.refresh(inst)
        iid = inst.id
        iname = inst.name
    _audit(request, inst, "instance.add", detail=f"注册实例 {name}")
    return {"id": iid, "name": iname, "token_ok": True}


@app.delete("/api/instances/{instance_id}")
async def delete_instance(request: Request, instance_id: int,
                          _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
        s.delete(inst)
        s.commit()
        iname = inst.name
    _audit(request, inst, "instance.delete", detail=f"删除实例 {iname}")
    return {"ok": True}


@app.post("/api/instances/{instance_id}/reauth")
async def reauth_instance(request: Request, instance_id: int, body: ReauthIn,
                          _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
        base = inst.base_url
    client = InstanceClient(base)
    try:
        token = await client.auth_login(body.username.strip() or "admin",
                                        body.password)
    except InstanceError as e:
        raise HTTPException(400, f"实例登录失败: {e}")
    with get_session() as s:
        inst2 = s.get(ConsoleInstance, instance_id)
        inst2.token = token
        inst2.token_expires_at = datetime.utcnow() + timedelta(days=14)
        inst2.last_error = ""
        s.add(inst2)
        s.commit()
    _audit(request, inst2, "instance.reauth", detail=f"实例 {inst2.name} 重新授权")
    return {"ok": True, "token_ok": True}


# ── 实例状态 ──
@app.get("/api/instances/{instance_id}/status")
async def instance_status(request: Request, instance_id: int,
                          _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        online = await client.health()
        accounts = await client.list_accounts()
        monitors = await client.list_monitors()
        users = await client.list_users()
        with get_session() as s:
            i2 = s.get(ConsoleInstance, instance_id)
            if online:
                i2.last_ok_at = datetime.utcnow()
                i2.last_error = ""
            s.add(i2)
            s.commit()
            last_error = i2.last_error  # 会话内取值, 防分离实例回读
        _audit(request, inst, "instance.status", detail=f"实例 {inst.name}")
        return {
            "id": inst.id, "name": inst.name, "base_url": inst.base_url,
            "online": online, "account_count": len(accounts),
            "monitor_count": len(monitors),
            "user_count": (users or {}).get("count", 0),
            "last_error": last_error,
        }
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


# ── 远端用户管理 ──
@app.get("/api/instances/{instance_id}/users")
async def instance_users(request: Request, instance_id: int,
                         _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        data = await client.list_users()
        _audit(request, inst, "user.list", detail=f"实例 {inst.name}")
        return data
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


class RemoteUserIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"


@app.post("/api/instances/{instance_id}/users", status_code=201)
async def remote_create_user(request: Request, instance_id: int, body: RemoteUserIn,
                             _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        out = await client.create_user(body.username, body.password, body.role)
        _audit(request, inst, "user.create",
               detail=f"实例 {inst.name} 建号 {body.username} ({body.role})")
        return out
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


class RemoteUserPatch(BaseModel):
    enabled: bool | None = None
    role: str | None = None


@app.patch("/api/instances/{instance_id}/users/{user_id}")
async def remote_patch_user(request: Request, instance_id: int, user_id: int,
                            body: RemoteUserPatch,
                            _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        out = await client.patch_user(user_id, body.model_dump(exclude_none=True))
        _audit(request, inst, "user.update", detail=f"实例 {inst.name} 用户 #{user_id}")
        return out
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


class RemotePasswordIn(BaseModel):
    new_password: str


@app.post("/api/instances/{instance_id}/users/{user_id}/password")
async def remote_reset_password(request: Request, instance_id: int, user_id: int,
                                body: RemotePasswordIn,
                                _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        out = await client.reset_password(user_id, body.new_password)
        _audit(request, inst, "user.password_reset",
               detail=f"实例 {inst.name} 用户 #{user_id}")
        return out
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


@app.delete("/api/instances/{instance_id}/users/{user_id}")
async def remote_delete_user(request: Request, instance_id: int, user_id: int,
                             _admin: ConsoleUser = Depends(_admin_only)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        out = await client.delete_user(user_id)
        _audit(request, inst, "user.delete",
               detail=f"实例 {inst.name} 用户 #{user_id}")
        return out
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


# ── 远端风控(查看/调整)──
@app.get("/api/instances/{instance_id}/risk")
async def remote_risk_get(request: Request, instance_id: int,
                          _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        data = await client.get_risk_config()
        _audit(request, inst, "risk.get", detail=f"实例 {inst.name}")
        return data
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


@app.put("/api/instances/{instance_id}/risk")
async def remote_risk_put(request: Request, instance_id: int, body: dict,
                          _admin: ConsoleUser = Depends(_admin_only)):
    if not isinstance(body, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        out = await client.put_risk_config(body)
        _audit(request, inst, "risk.put", detail=f"实例 {inst.name} 风控配置更新")
        return out
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


# ── 远端审计查看 ──
@app.get("/api/instances/{instance_id}/audit-requests")
async def remote_audit_requests(request: Request, instance_id: int, limit: int = 100,
                                _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        data = await client.audit_requests(limit=limit)
        _audit(request, inst, "audit.requests", detail=f"实例 {inst.name}")
        return data
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


@app.get("/api/instances/{instance_id}/audit-ops")
async def remote_audit_ops(request: Request, instance_id: int, limit: int = 100,
                           _u: ConsoleUser = Depends(_any_user)):
    with get_session() as s:
        inst = _instance_or_404(s, instance_id)
    client = _require_valid_token(request, inst)
    try:
        data = await client.audit_ops(limit=limit)
        _audit(request, inst, "audit.ops", detail=f"实例 {inst.name}")
        return data
    except InstanceError as e:
        raise _map_instance_error(request, inst, e)


# ── 控制台操作审计查看(本控制台自己的)──
@app.get("/api/console/audit")
async def console_audit(limit: int = 200,
                        _admin: ConsoleUser = Depends(_admin_only)):
    limit = max(1, min(limit, 1000))
    with get_session() as s:
        rows = s.exec(select(ConsoleAudit).order_by(
            ConsoleAudit.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "username": r.username,
            "instance_name": r.instance_name, "action": r.action,
            "ok": r.ok, "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


# ── 前端 ──
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="console-static")