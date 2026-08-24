"""客户端接入控制面: 主动注册 + 轮询(心跳/审计上报/取令执行/回执) + 登录验证委派。

流量方向只有 客户端→控制面(内网/NAT 无入口也适用):
  register → 换 client_token; poll(每 interval) → 上报状态+审计增量+指令回执,
  取走待办指令(risk.set 等)并执行后回执; verify → 本机登录交控制面校验(严格版)。
被停用(disabled)时: 本机登录拒绝 + 心跳继续(以感知恢复启用)。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx

from .config import Config

log = logging.getLogger("creatorhub.registry")

DISABLED = False                       # 被控制面停用标记
_pending_receipts: list[dict] = []     # 待回执(执行指令后下轮上报)
_last_request_audit_id = 0             # 审计增量游标
_last_op_audit_id = 0


def registry_enabled(cfg: Config) -> bool:
    cc = cfg.console
    return bool(cc.enabled and cc.url.strip() and cc.username.strip()
                and cc.password)


def client_disabled() -> bool:
    return DISABLED


def _client(cfg: Config) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=cfg.console.url.strip().rstrip("/"),
                             timeout=15.0, trust_env=False)


def _version_string() -> str:
    try:
        import app
        return str(getattr(app, "__version__", "1.0"))
    except Exception:
        return "1.0"


async def verify_login(cfg: Config, username: str, password: str) -> bool:
    """本机登录委派: 严格模式, 控制面校验账号密码(与注册用同一账号)。"""
    try:
        async with _client(cfg) as cli:
            r = await cli.post("/api/clients/verify", json={
                "username": username, "password": password})
            try:
                body = r.json()
            except Exception:
                body = {}
            return r.status_code == 200 and bool(body.get("ok"))
    except Exception as e:
        log.warning("控制面验证不可达: %r", e)
        return False


async def _register(cfg: Config) -> str:
    cc = cfg.console
    async with _client(cfg) as cli:
        r = await cli.post("/api/clients/register", json={
            "username": cc.username.strip(),
            "password": cc.password,
            "version": _version_string(),
            "note": "CreatorHub " + _version_string(),
        })
        if r.status_code != 200:
            raise RuntimeError(f"控制面注册失败 HTTP {r.status_code}: "
                               f"{r.text[:200]}")
        return r.json()["client_token"]


def _collect_status(cfg: Config) -> dict:
    from .db import get_session
    from .models import DouyinAccount, MonitorTarget
    from sqlmodel import select, func
    try:
        with get_session() as s:
            accounts = s.exec(select(func.count()).select_from(
                DouyinAccount)).one()
            monitors = s.exec(select(func.count()).select_from(
                MonitorTarget)).one()
        return {"version": _version_string(), "accounts": int(accounts),
                "monitors": int(monitors), "time": datetime.now().isoformat()}
    except Exception as e:
        log.warning("状态采集失败: %r", e)
        return {"version": _version_string()}


def _collect_audit_delta(cfg: Config, limit: int = 50) -> list[dict]:
    """自上次上报以来的请求/操作审计增量(进程内存游标)。"""
    global _last_request_audit_id, _last_op_audit_id
    from .db import get_session
    from .models import RequestAudit, RiskAdminAudit
    from sqlmodel import select
    out: list[dict] = []
    try:
        with get_session() as s:
            reqs = s.exec(select(RequestAudit).where(
                RequestAudit.id > _last_request_audit_id).order_by(
                RequestAudit.id).limit(limit)).all()
            for r in reqs:
                _last_request_audit_id = max(_last_request_audit_id, r.id or 0)
                out.append({"kind": "request", "action": f"{r.method} {r.path}",
                            "username": r.username or "",
                            "detail": f"HTTP {r.status_code}",
                            "ok": r.status_code < 400,
                            "created_at": r.created_at.isoformat()
                            if r.created_at else None})
            ops = s.exec(select(RiskAdminAudit).where(
                RiskAdminAudit.id > _last_op_audit_id).order_by(
                RiskAdminAudit.id).limit(limit)).all()
            for r in ops:
                _last_op_audit_id = max(_last_op_audit_id, r.id or 0)
                out.append({"kind": "op", "action": r.action,
                            "username": r.actor or "",
                            "detail": (r.detail or "")[:200], "ok": True,
                            "created_at": r.created_at.isoformat()
                            if r.created_at else None})
    except Exception as e:
        log.warning("审计增量采集失败: %r", e)
    return out[:limit]


async def _execute_command(cmd: dict) -> tuple[bool, str]:
    """执行控制面指令。返回 (ok, result)。"""
    op = cmd.get("op") or ""
    params = cmd.get("params") or {}
    try:
        if op == "risk.set":
            from .risk_admin import apply_risk_settings, save_risk_settings
            from .config import load_config
            local_cfg = load_config()
            apply_risk_settings(local_cfg, params or {})
            save_risk_settings(local_cfg)
            return True, "risk 配置已应用"
        return False, f"未知指令: {op}"
    except Exception as e:
        log.exception("指令执行失败: %s", op)
        return False, f"{type(e).__name__}: {str(e)[:200]}"


async def _poll_once(cfg: Config, token: str) -> bool:
    """单次轮询: 上报状态+审计+回执, 取令执行并回执。返回是否被停用。"""
    global DISABLED
    payload = {
        "status": _collect_status(cfg),
        "audit": _collect_audit_delta(cfg),
        "receipts": list(_pending_receipts),
    }
    _pending_receipts.clear()
    async with _client(cfg) as cli:
        r = await cli.post("/api/clients/poll", json=payload,
                           headers={"X-Client-Token": token})
        if r.status_code != 200:
            log.warning("轮询异常 HTTP %s: %s", r.status_code, r.text[:200])
            return DISABLED
        body = r.json()
    DISABLED = bool(body.get("disabled"))
    for cmd in body.get("commands") or []:
        ok, result = await _execute_command(cmd)
        _pending_receipts.append({
            "command_id": cmd.get("id"), "status": "done" if ok else "failed",
            "result": result,
        })
    return DISABLED


async def run(cfg: Config) -> None:
    """注册 + 循环轮询(后台任务, 断线自动重试/重新注册)。"""
    if not registry_enabled(cfg):
        return
    interval = max(5, int(cfg.console.poll_interval_seconds or 30))
    token = ""
    while True:
        try:
            if not token:
                token = await _register(cfg)
                log.info("已注册到控制面: %s", cfg.console.username)
            await _poll_once(cfg, token)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("控制面通信失败(%.0fs 后重试): %r", interval, e)
            token = ""   # 令牌可能失效, 下轮重新注册
        await asyncio.sleep(interval)