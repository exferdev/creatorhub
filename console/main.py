"""控制面(独立管理后台)主服务 — 客户端主动上报/轮询模型。

独立部署:  python -m uvicorn console.main:app --host 127.0.0.1 --port 8100
架构: 一台 CreatorHub 客户端 = 一个 ClientAccount(username); 客户端主动注册并向
本服务轮询(内网无入口): 上报状态/推送审计/取走指令; 本机登录验证也集中于本服务。
流量方向只有 客户端→控制面, 控制面永不主动连客户端。
v1 指令: risk.set(下发风控配置); 停用/启用与重置密码直接作用于账号状态。
"""
from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from .console_auth import (auth_backend, auth_bypass_enabled, current_user,
                           ensure_bootstrap_console_admin, fastapi_users,
                           hash_password, require_roles)
from .db import get_session
from .models import (ClientAccount, ClientAudit, ClientCommand, ConsoleAudit,
                     ConsoleUser)

_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "console" / "data" / "console.db")


def console_db_path() -> str:
    """实时取数据库路径(测试可随时换库, 不受 import 时点影响)。"""
    return os.environ.get("CONSOLE_DB_PATH") or _DEFAULT_DB
WEB_DIR = Path(__file__).resolve().parent / "web"
POLL_INTERVAL = 30          # 建议轮询间隔(秒), 客户端可覆盖心跳
AUDIT_BATCH_LIMIT = 500     # 单次轮询审计增量上限


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if ensure_bootstrap_console_admin(console_db_path()):
            print("[console] 控制台管理员已初始化(admin), 初始密码见上")
    except Exception as e:
        print(f"[console] 初始化失败(不影响启动): {e!r}")
    # 挂载 starlette-admin 后台(引擎已就绪; starlette-admin 缺省时跳过)
    try:
        from .db import _engine
        if _engine is not None and _HAS_ADMIN_DEPS \
                and not getattr(app, "_console_admin_mounted", False):
            from .admin import build_admin
            build_admin(_engine).mount_to(app)
            app._console_admin_mounted = True
            print("[console] 后台管理 /admin 已挂载(starlette-admin)")
    except Exception as e:
        print(f"[console] 后台管理挂载跳过(需要 starlette-admin): {e!r}")
    yield


app = FastAPI(title="CreatorHub Console", lifespan=lifespan)

# ── 后台会话(条件依赖: 主 venv 无 starlette-admin 时跳过, 不影响 API)──
try:
    from starlette.middleware.sessions import SessionMiddleware
    from .admin import ADMIN_SECRET
    app.add_middleware(SessionMiddleware, secret_key=ADMIN_SECRET,
                       https_only=False)
    _HAS_ADMIN_DEPS = True
except Exception:  # 无 starlette-admin/itsdangerous 环境: 仅无 /admin
    _HAS_ADMIN_DEPS = False
app.include_router(fastapi_users.get_auth_router(auth_backend),
                   prefix="/api/console/auth")

manage_roles = Depends(require_roles("admin", "operator"))
view_roles = Depends(require_roles("admin", "operator", "viewer"))


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


# ── 全局守卫: 白名单外必须携带控制台令牌 ──
OPEN_EXACT = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
OPEN_PREFIX = ("/static",
               "/api/console/auth/",
               "/api/clients/",    # 客户端注册/轮询/验证: 免控制台登录(自有认证)
               "/admin",)          # starlette-admin 后台: 自带会话鉴权(admin/operator)


@app.middleware("http")
async def console_guard(request: Request, call_next):
    if auth_bypass_enabled():
        return await call_next(request)
    path = request.url.path
    if path in OPEN_EXACT or path.startswith(OPEN_PREFIX):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    from .models import ConsoleAccessToken as _CAT
    user = None
    if token:
        with get_session() as s:
            row = s.get(_CAT, token)
            if row is not None and \
                    datetime.utcnow() - row.created_at <= timedelta(days=14):
                user = s.get(ConsoleUser, row.user_id)
    if user is None or not user.is_active:
        return JSONResponse({"detail": "未登录或令牌无效"}, status_code=401)
    request.state.user = user
    return await call_next(request)


# ── 审计助手 ──
def _audit(request: Request, client, action: str,
           ok: bool = True, detail: str = ""):
    # client 可为模型或含 id/username 的对象; 值须在调用前读取(防分离实例过期)
    u = getattr(request.state, "user", None)
    try:
        with get_session() as s:
            s.add(ConsoleAudit(
                user_id=getattr(u, "id", None),
                username=getattr(u, "username", "") or "",
                client_id=getattr(client, "id", None),
                client_name=getattr(client, "username", "") or "",
                action=action, ok=ok, detail=detail[:500]))
            s.commit()
    except Exception:
        pass


def _client_by_username(session, username: str) -> ClientAccount:
    acc = session.exec(select(ClientAccount).where(
        ClientAccount.username == username)).first()
    if acc is None:
        raise HTTPException(404, "客户端不存在")
    return acc


def _client_out(acc: ClientAccount) -> dict:
    online = acc.last_seen_at is not None and \
        datetime.utcnow() - acc.last_seen_at <= timedelta(seconds=POLL_INTERVAL * 3)
    return {
        "id": acc.id, "username": acc.username, "note": acc.note,
        "disabled": acc.disabled, "online": online,
        "version": acc.version,
        "last_seen_at": acc.last_seen_at.isoformat() if acc.last_seen_at else None,
        "last_error": acc.last_error,
        "status": json.loads(acc.status_json or "{}"),
        "pending_commands": _pending_count(acc.id),
    }


def _pending_count(client_id: int) -> int:
    with get_session() as s:
        return len(s.exec(select(ClientCommand).where(
            ClientCommand.client_id == client_id,
            ClientCommand.status == "pending")).all())


# ═══════════════════ 客户端侧接口(免控制台登录)═══════════════════

class RegisterIn(BaseModel):
    username: str
    password: str
    version: str = ""
    note: str = ""


@app.post("/api/clients/register")
async def client_register(body: RegisterIn):
    """客户端启动注册: 首次创建账号并签发轮询令牌; 已存在则校验密码。"""
    from fastapi_users.password import PasswordHelper
    username = body.username.strip()
    if not username or len(username) > 40:
        raise HTTPException(400, "用户名须为 1~40 字符")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    ph = PasswordHelper()
    with get_session() as s:
        acc = s.exec(select(ClientAccount).where(
            ClientAccount.username == username)).first()
        if acc is None:
            acc = ClientAccount(
                username=username,
                password_hash=ph.hash(body.password),
                client_token=secrets.token_urlsafe(32),
                version=body.version[:40], note=body.note.strip()[:200])
            s.add(acc)
            s.commit()
            print(f"[console] 客户端主动注册: {username}")
        else:
            if acc.disabled:
                raise HTTPException(403, "客户端已被停用")
            ok, _ = ph.verify_and_update(body.password, acc.password_hash)
            if not ok:
                raise HTTPException(401, "客户端账号或密码错误")
            acc.version = body.version[:40] or acc.version
            if not acc.client_token:
                acc.client_token = secrets.token_urlsafe(32)
            s.add(acc)
            s.commit()
        token = acc.client_token
        cid, cname = acc.id, acc.username
    return {"ok": True, "client_token": token, "poll_interval": POLL_INTERVAL,
            "id": cid, "username": cname}


class PollIn(BaseModel):
    status: dict | None = None
    audit: list[dict] = PydanticField(default_factory=list)
    receipts: list[dict] = PydanticField(default_factory=list)


@app.post("/api/clients/poll")
async def client_poll(request: Request, body: PollIn):
    """客户端心跳轮询: 上报状态/审计增量/指令回执, 取走待办指令。"""
    token = request.headers.get("X-Client-Token", "").strip()
    if not token:
        raise HTTPException(401, "缺少 X-Client-Token")
    with get_session() as s:
        acc = s.exec(select(ClientAccount).where(
            ClientAccount.client_token == token)).first()
        if acc is None:
            raise HTTPException(401, "客户端令牌无效, 请重新注册")
        if acc.disabled:
            acc.last_seen_at = datetime.utcnow()
            s.add(acc)
            s.commit()
            return {"ok": True, "disabled": True,
                    "commands": [], "poll_interval": POLL_INTERVAL}
        acc.last_seen_at = datetime.utcnow()
        if body.status:
            acc.status_json = json.dumps(body.status, ensure_ascii=False)[:4000]
            acc.version = str(body.status.get("version") or acc.version)[:40]
        acc.last_error = ""
        s.add(acc)
        s.commit()
        cid, cname = acc.id, acc.username
    # 审计增量入库(封顶防滥用)
    batch = (body.audit or [])[:AUDIT_BATCH_LIMIT]
    if batch:
        with get_session() as s:
            for item in batch:
                if not isinstance(item, dict):
                    continue
                s.add(ClientAudit(
                    client_id=cid, client_name=cname,
                    kind=str(item.get("kind") or "request")[:20],
                    action=str(item.get("action") or "")[:200],
                    username=str(item.get("username") or "")[:64],
                    detail=str(item.get("detail") or "")[:500],
                    ok=bool(item.get("ok", True))))
            s.commit()
    # 指令回执
    receipts = (body.receipts or [])[:200]
    if receipts:
        with get_session() as s:
            for rec in receipts:
                cmd = s.get(ClientCommand, int(rec.get("command_id") or 0)) \
                    if str(rec.get("command_id") or "").isdigit() else None
                if cmd is None or cmd.client_id != cid:
                    continue
                cmd.status = "done" if rec.get("status") == "done" else "failed"
                cmd.result = str(rec.get("result") or "")[:500]
                cmd.done_at = datetime.utcnow()
                s.add(cmd)
            s.commit()
    # 取出待办指令
    with get_session() as s:
        cmds = s.exec(select(ClientCommand).where(
            ClientCommand.client_id == cid,
            ClientCommand.status == "pending").order_by(
            ClientCommand.id).limit(20)).all()
        out = [{"id": c.id, "op": c.op,
                "params": json.loads(c.params or "{}")} for c in cmds]
    return {"ok": True, "disabled": False, "commands": out,
            "poll_interval": POLL_INTERVAL}


class VerifyIn(BaseModel):
    username: str
    password: str


@app.post("/api/clients/verify")
async def client_verify(body: VerifyIn):
    """客户端本机登录验证(严格版: 一律走控制面; 停用则拒绝)。"""
    from fastapi_users.password import PasswordHelper
    ph = PasswordHelper()
    with get_session() as s:
        acc = s.exec(select(ClientAccount).where(
            ClientAccount.username == body.username.strip())).first()
        if acc is None:
            return JSONResponse({"ok": False, "detail": "账号不存在"}, status_code=401)
        if acc.disabled:
            return JSONResponse({"ok": False, "detail": "客户端已被停用"},
                                status_code=403)
        ok, _ = ph.verify_and_update(body.password, acc.password_hash)
        if not ok:
            return JSONResponse({"ok": False, "detail": "账号或密码错误"},
                                status_code=401)
    return {"ok": True, "username": acc.username}


# ═══════════════════ 控制台管理接口(Console 用户)═══════════════════

@app.get("/api/admin/clients")
async def list_clients(_u: ConsoleUser = view_roles):
    with get_session() as s:
        accs = s.exec(select(ClientAccount).order_by(
            ClientAccount.id)).all()
        return {"count": len(accs), "clients": [_client_out(a) for a in accs]}


@app.get("/api/admin/clients/{username}")
async def client_detail(username: str,
                        _u: ConsoleUser = view_roles):
    with get_session() as s:
        acc = _client_by_username(s, username)
        return _client_out(acc)


@app.post("/api/admin/clients/{username}/disable")
async def disable_client(request: Request, username: str,
                         _admin: ConsoleUser = manage_roles):
    with get_session() as s:
        acc = _client_by_username(s, username)
        acc.disabled = True
        s.add(acc)
        s.commit()
        _cid, _cname = acc.id, acc.username
    _audit(request, type("C", (), {"id": _cid, "username": _cname})(),
           "client.disable", detail=f"停用客户端 {username}")
    return {"ok": True, "disabled": True}


@app.post("/api/admin/clients/{username}/enable")
async def enable_client(request: Request, username: str,
                        _admin: ConsoleUser = manage_roles):
    with get_session() as s:
        acc = _client_by_username(s, username)
        acc.disabled = False
        s.add(acc)
        s.commit()
        _cid, _cname = acc.id, acc.username
    _audit(request, type("C", (), {"id": _cid, "username": _cname})(),
           "client.enable", detail=f"启用客户端 {username}")
    return {"ok": True, "disabled": False}


class ResetPasswordIn(BaseModel):
    new_password: str


@app.post("/api/admin/clients/{username}/reset-password")
async def reset_client_password(request: Request, username: str,
                                body: ResetPasswordIn,
                                _admin: ConsoleUser = manage_roles):
    from fastapi_users.password import PasswordHelper
    if len(body.new_password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    ph = PasswordHelper()
    with get_session() as s:
        acc = _client_by_username(s, username)
        acc.password_hash = ph.hash(body.new_password)
        s.add(acc)
        s.commit()
        _cid, _cname = acc.id, acc.username
    _audit(request, type("C", (), {"id": _cid, "username": _cname})(),
           "client.password_reset", detail=f"重置客户端 {username} 密码")
    return {"ok": True}


class CommandIn(BaseModel):
    op: str
    params: dict = PydanticField(default_factory=dict)


@app.post("/api/admin/clients/{username}/command")
async def send_client_command(request: Request, username: str, body: CommandIn,
                              _admin: ConsoleUser = manage_roles):
    if body.op not in ("risk.set",):
        raise HTTPException(400, f"暂不支持指令: {body.op}")
    with get_session() as s:
        acc = _client_by_username(s, username)
        cmd = ClientCommand(
            client_id=acc.id, client_name=acc.username, op=body.op,
            params=json.dumps(body.params, ensure_ascii=False)[:4000])
        s.add(cmd)
        s.commit()
        cid = cmd.id
        _cname = acc.username
    _audit(request, type("C", (), {"id": acc.id, "username": _cname})(),
           "client.command", detail=f"下发 {body.op} 至 {username}")
    return {"ok": True, "command_id": cid, "status": "pending"}


@app.get("/api/admin/clients/{username}/audit")
async def client_audit(username: str, limit: int = 200,
                       _u: ConsoleUser = view_roles):
    limit = max(1, min(limit, 1000))
    with get_session() as s:
        acc = _client_by_username(s, username)
        rows = s.exec(select(ClientAudit).where(
            ClientAudit.client_id == acc.id).order_by(
            ClientAudit.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "kind": r.kind, "action": r.action, "username": r.username,
            "detail": r.detail, "ok": r.ok,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


@app.get("/api/admin/clients/{username}/commands")
async def client_commands(username: str, limit: int = 100,
                          _u: ConsoleUser = view_roles):
    limit = max(1, min(limit, 500))
    with get_session() as s:
        acc = _client_by_username(s, username)
        rows = s.exec(select(ClientCommand).where(
            ClientCommand.client_id == acc.id).order_by(
            ClientCommand.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "op": r.op, "params": json.loads(r.params or "{}"),
            "status": r.status, "result": r.result,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "done_at": r.done_at.isoformat() if r.done_at else None,
        } for r in rows]


@app.get("/api/console/audit")
async def console_audit(limit: int = 200,
                        _admin: ConsoleUser = Depends(require_roles("admin"))):
    limit = max(1, min(limit, 1000))
    with get_session() as s:
        rows = s.exec(select(ConsoleAudit).order_by(
            ConsoleAudit.id.desc()).limit(limit)).all()
        return [{
            "id": r.id, "username": r.username, "client_name": r.client_name,
            "action": r.action, "ok": r.ok, "detail": r.detail,
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
# (starlette-admin 后台与 SessionMiddleware 在 lifespan 内挂载, 缺依赖时跳过)