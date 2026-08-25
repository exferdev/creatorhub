"""M5 告警与保留策略。

触发事件(后台每 60s 检查):
  - 客户端离线(状态翻转时发一次, 恢复后重置)
  - 任务失败(每条 failed 指令发一次)
  - 客户端签名命中率低于阈值(每客户端 30 分钟窗口去重)
通知渠道 = AppriseChannel(表) 中的 notify_urls(Apprise 协议, 支持邮件/Telegram/
钉钉/Webhook 等)。保留策略: 每小时清理过期审计/指令(默认 90 天, 可配)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import select

from .db import get_session
from .models import AppriseChannel, ClientAccount, ClientCommand, Setting

log = logging.getLogger("creatorhub.console.alerts")

# 默认设置
DEFAULTS = {
    "offline_alert_enabled": "1",
    "offline_after_seconds": "180",
    "task_fail_alert_enabled": "1",
    "sign_health_alert_enabled": "1",
    "sign_ok_rate_threshold": "0.8",
    "retention_days": "90",
    "alert_interval_seconds": "60",
}

_ALARM_STATE = {   # 离线告警: client_id -> bool(已告警); 签名告警: client_id -> ts
    "offline": {},
    "sign": {},
}
_ALARMED_COMMAND_IDS = set()   # 已告警的失败指令 id(防重发, 进程内)


def get_setting(key: str, default: str = "") -> str:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row is not None else default


def get_int(key: str, fallback: int) -> int:
    try:
        return int(get_setting(key, str(fallback)))
    except (TypeError, ValueError):
        return fallback


def set_setting(key: str, value: str):
    with get_session() as s:
        row = s.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
            s.add(row)
        else:
            row.value = value
            row.updated_at = datetime.utcnow()
        s.commit()


def channels() -> list[AppriseChannel]:
    with get_session() as s:
        rows = s.exec(select(AppriseChannel).where(
            AppriseChannel.enabled.is_(True))).all()
        return list(rows)


def send_alert(title: str, body: str) -> bool:
    """发通知到全部启用渠道。返回是否至少一条成功。"""
    import apprise as _apprise
    ap = _apprise.Apprise()
    added = 0
    for ch in channels():
        urls = [u.strip() for u in (ch.notify_urls or "").splitlines()
               if u.strip()]
        for u in urls:
            if ap.add(u):
                added += 1
    if not added:
        return False
    try:
        return ap.notify(title=title, body=body)
    except Exception as e:
        log.warning("告警发送失败: %r", e)
        return False


def _enabled(key: str) -> bool:
    val = get_setting(key, DEFAULTS.get(key, "0"))
    return val.strip().lower() in ("1", "true", "yes")


async def check_client_offline(now: datetime) -> int:
    """离线状态翻转告警。返回本次告警数。"""
    if not _enabled("offline_alert_enabled"):
        return 0
    after = get_int("offline_after_seconds", 180)
    sent = 0
    with get_session() as s:
        accs = s.exec(select(ClientAccount)).all()
        for a in accs:
            if a.disabled:
                continue
            offline = a.last_seen_at is None or \
                now - a.last_seen_at > timedelta(seconds=after)
            flagged = _ALARM_STATE["offline"].get(a.id, False)
            if offline and not flagged:
                _ALARM_STATE["offline"][a.id] = True
                s.commit()
                send_alert("CreatorHub 告警: 客户端离线",
                           f"客户端 {a.username} 已超过 {after}s 未上报心跳"
                           f"(最后心跳: {a.last_seen_at})")
                sent += 1
            elif not offline and flagged:
                _ALARM_STATE["offline"][a.id] = False
        s.commit()
    return sent


async def check_task_failures(now: datetime) -> int:
    """新出现的 failed 指令告警(每 id 一次)。"""
    if not _enabled("task_fail_alert_enabled"):
        return 0
    sent = 0
    with get_session() as s:
        rows = s.exec(select(ClientCommand).where(
            ClientCommand.status == "failed").order_by(
            ClientCommand.id.desc()).limit(50)).all()
        for c in rows:
            if c.id in _ALARMED_COMMAND_IDS:
                continue
            _ALARMED_COMMAND_IDS.add(c.id)
            if len(_ALARMED_COMMAND_IDS) > 2000:
                _ALARMED_COMMAND_IDS.clear()
            s.commit()
            send_alert("CreatorHub 告警: 任务失败",
                       f"客户端 {c.client_name} 任务失败 #{c.id} {c.op}: "
                       f"{(c.result or '')[:200]}")
            sent += 1
        s.commit()
    return sent


async def check_sign_health(now: datetime) -> int:
    """客户端签名命中率低于阈值告警(每客户端 30 分钟去重)。"""
    if not _enabled("sign_health_alert_enabled"):
        return 0
    threshold = float(get_setting("sign_ok_rate_threshold", "0.8") or 0.8)
    sent = 0
    with get_session() as s:
        accs = s.exec(select(ClientAccount)).all()
        for a in accs:
            st = __import__("json").loads(a.status_json or "{}")
            sh = st.get("sign_health") or {}
            worst = None
            for platform, info in sh.items():
                rate = float(info.get("ok_rate") or 1.0)
                if info.get("errors", 0) > 0 and rate < threshold:
                    worst = (platform, rate)
                    break
            if worst is None:
                continue
            last = _ALARM_STATE["sign"].get(a.id, 0.0)
            import time as _t
            if _t.time() - last < 1800:   # 30 分钟去重
                continue
            _ALARM_STATE["sign"][a.id] = _t.time()
            s.commit()
            send_alert("CreatorHub 告警: 签名命中率异常",
                       f"客户端 {a.username} {worst[0]} 签名成功率 "
                       f"{worst[1]:.0%} < {threshold:.0%}")
            sent += 1
        s.commit()
    return sent


async def run_alert_loop():
    """后台告警检查(默认 60s 间隔)。"""
    import asyncio as _a
    while True:
        try:
            now = datetime.utcnow()
            await check_client_offline(now)
            await check_task_failures(now)
            await check_sign_health(now)
        except Exception as e:
            log.warning("告警检查失败: %r", e)
        await _a.sleep(get_int("alert_interval_seconds", 60))


async def run_retention_once():
    """执行一轮保留清理(可单测)。"""
    days = get_int("retention_days", 90)
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        from .models import (ClientAudit, ClientCommand, ConsoleAccessToken,
                             ConsoleAudit)
        for model in (ClientAudit, ConsoleAudit):
            rows = s.exec(select(model).where(
                model.created_at < cutoff)).all()
            for r in rows:
                s.delete(r)
        # 已回执且过期的指令
        rows = s.exec(select(ClientCommand).where(
            ClientCommand.status.in_(("done", "failed")),
            ClientCommand.created_at < cutoff)).all()
        for r in rows:
            s.delete(r)
        # 过期登录令牌(>14 天)
        old_tokens = s.exec(select(ConsoleAccessToken).where(
            ConsoleAccessToken.created_at < cutoff)).all()
        for r in old_tokens:
            s.delete(r)
        s.commit()


async def run_retention_loop():
    """保留策略: 每小时清理过期审计/指令(默认 90 天)。"""
    import asyncio as _a
    while True:
        try:
            await run_retention_once()
        except Exception as e:
            log.warning("保留清理失败: %r", e)
        await _a.sleep(3600)