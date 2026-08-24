"""签名服务调用统计(自启动累计 + 最近 60s 窗口), 随控制面轮询上报 sign_health。

各平台独立计数; Console 算法中心"客户端签名命中健康"面板数据源。
"""
from __future__ import annotations

import time

_WINDOW = 60.0          # 最近窗口(秒)
_SAMPLE_LIMIT = 200     # 单平台耗时样本上限(p95 用)

_STATS: dict = {}       # platform -> {since, total, ok, samples:[(ts,ms)], last_error}


def record(platform: str, ok: bool, ms: float = 0.0, error: str = ""):
    now = time.time()
    st = _STATS.setdefault(platform, {
        "since": now, "total": 0, "ok": 0, "samples": [], "last_error": ""})
    st["total"] += 1
    if ok:
        st["ok"] += 1
        st["samples"].append((now, ms))
        if len(st["samples"]) > _SAMPLE_LIMIT:
            st["samples"].pop(0)
    else:
        st["last_error"] = (error or "")[:200]


def _p95(points):
    if not points:
        return None
    vals = sorted(p[1] for p in points)
    idx = min(len(vals) - 1, max(0, int(len(vals) * 0.95) - 1))
    return round(vals[idx], 1)


def snapshot() -> dict:
    """返回 {platform: {total, ok_rate, errors, avg_ms, p95_ms, last_error}}。"""
    now = time.time()
    out: dict = {}
    for p, st in _STATS.items():
        cutoff = now - _WINDOW
        recent = [s for s in st["samples"] if s[0] >= cutoff]
        # 窗口内失败率: 失败无时间戳, 以累计率近似(服务端为趋势用途)
        ok_rate = (st["ok"] / st["total"]) if st["total"] else 1.0
        out[p] = {
            "total": st["total"],
            "ok_rate": round(ok_rate, 4),
            "errors": st["total"] - st["ok"],
            "avg_ms": round(sum(s[1] for s in recent) / len(recent), 1) if recent else None,
            "p95_ms": _p95(recent),
            "last_error": st["last_error"],
        }
    return out


def reset():
    _STATS.clear()