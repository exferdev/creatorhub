"""抖音 msToken 生成器 — strData 重放 + 缓存策略。

架构:
  S0: strData 重放 (纯 Python requests, 最稳定)
  Cache: 7 天缓存 (优先级高于 S0，不强制刷新时直接返回)
  S2: mssdk API 两步交换 (需有效 cookie)

弃用 douyin-ops 的 S1(CDP) 和 S3(curl_cffi)，因为 CreatorHub 不做独立的指纹浏览器 CDP。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import time
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "ms_token_cache.json")
STRDATA_CACHE_FILE = os.path.join(DATA_DIR, "strdata_cache.json")

MSSDK_R_TOKEN_URL = "https://mssdk.bytedance.com/web/r/token"
MSSDK_COMMON_URL = "https://mssdk.bytedance.com/web/common"
MSSDK_BODY_TEMPLATE = {
    "magic": 538969122, "version": 1, "dataType": 8,
    "strData": "", "tspFromClient": 0, "ulr": 0,
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36")

_FAKE_CHARSET = string.ascii_letters + string.digits + "=_-"


def gen_false_ms_token(length: int = 126) -> str:
    """本地伪造 msToken(读取公开作品列表通常够用, 非签名)。"""
    return "".join(random.choice(_FAKE_CHARSET) for _ in range(length))


# ── strData 画像参数化(每账号指纹不同, 防多账号共用同一指纹被风控关联)──
_TZ_OFFSET_CACHE: dict = {}
_MEM_OPTIONS = (4, 6, 8, 12, 16, 32)
_CORE_OPTIONS = (4, 8, 12, 16, 24)


def _tz_offset_hours(timezone_id: str) -> int | None:
    """按 IANA 时区名算当前 UTC 偏移小时(worker 的 timezone 参数)。"""
    tz_id = (timezone_id or "").strip() or "Asia/Shanghai"
    if tz_id in _TZ_OFFSET_CACHE:
        return _TZ_OFFSET_CACHE[tz_id]
    try:
        from zoneinfo import ZoneInfo
        offset = int(datetime.now(ZoneInfo(tz_id)).utcoffset().total_seconds() // 3600)
        _TZ_OFFSET_CACHE[tz_id] = offset
        return offset
    except Exception:
        return None


def _seed_pick(seed: str, salt: str, options) -> int:
    digest = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).hexdigest()
    return options[int(digest[:2], 16) % len(options)]


def _default_db_dir() -> str:
    """指纹库根目录: 优先环境变量, 其次既有默认路径(不存在则空→跳过文件解析)。"""
    d = os.environ.get("FINGERPRINT_DB_DIR", "").strip()
    if not d and os.path.exists("E:/fingerprint-db"):
        d = "E:/fingerprint-db"
    return d


def build_strdata_profile(ua: str = "", *, identity=None, navigator=None) -> dict:
    """按账号画像构建 strData 参数(每账号指纹不同)。

    navigator 为空且 identity 携带 shardx_id/fingerprint_name/fp_seed 时,
    会尝试只读解析该账号真实指纹 profile 的 navigator(不启动浏览器);
    解析失败/无身份时回退 fp_seed 确定性派生 + UA。worker 白名单外字段会被忽略。
    """
    profile: dict = {"ua": ua or ""}
    if identity is None:
        return profile
    seed = str(getattr(identity, "fp_seed", "") or "")

    if navigator is None:
        try:
            from app.browser.fingerprint_store import resolve_navigator
            navigator = resolve_navigator(
                _default_db_dir(),
                shardx_id=getattr(identity, "shardx_id", "") or "",
                fingerprint_name=getattr(identity, "fingerprint_name", "") or "",
                fp_seed=seed,
                platform=getattr(identity, "os", "") or "",
            )
        except Exception:
            navigator = None

    if navigator and isinstance(navigator, dict):
        mem = navigator.get("device_memory")
        cores = navigator.get("hardware_concurrency")
        plat = navigator.get("platform_value")
        lang = navigator.get("language")
    else:
        mem = cores = plat = lang = None

    if not mem:
        mem = getattr(identity, "memory_gb", 0) or _seed_pick(seed, "mem", _MEM_OPTIONS)
    if not cores:
        cores = getattr(identity, "cpu_cores", 0) or _seed_pick(seed, "cores", _CORE_OPTIONS)
    profile["deviceMemory"] = int(mem)
    profile["hardwareConcurrency"] = int(cores)
    if plat:
        profile["platform"] = str(plat)
    if not lang:
        lang = str(getattr(identity, "locale", "") or "zh-CN").strip()
    if lang:
        profile["language"] = lang
    tz = _tz_offset_hours(getattr(identity, "timezone_id", ""))
    if tz is not None:
        profile["timezone"] = tz
    if getattr(identity, "viewport_w", 0) and getattr(identity, "viewport_h", 0):
        profile["viewport_w"] = int(identity.viewport_w)
        profile["viewport_h"] = int(identity.viewport_h)
    profile["screen"] = {"colorDepth": 24}
    return profile


def _strdata_cache_key(ua: str = "", identity=None) -> str:
    """strData 缓存按账号隔离: 避免账号 A 的指纹被账号 B 复用。"""
    seed = str(getattr(identity, "fp_seed", "") or "")
    raw = f"{ua or ''}|{seed}".strip("|")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _write_json_secure(path: str, data: dict):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except (PermissionError, OSError):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_cached_ms_token(cookies_domain: str = "creator.douyin.com") -> str:
    cache = _load_json(CACHE_FILE)
    entry = cache.get(cookies_domain, {})
    token = entry.get("token", "")
    saved_at = entry.get("saved_at", "")
    if token and saved_at:
        try:
            saved_time = datetime.fromisoformat(saved_at)
            if datetime.now() - saved_time < timedelta(days=7):
                return token
        except Exception:
            pass
    return ""


def set_cached_ms_token(token: str, cookies_domain: str = "creator.douyin.com"):
    cache = _load_json(CACHE_FILE)
    cache[cookies_domain] = {"token": token, "saved_at": datetime.now().isoformat()}
    _write_json_secure(CACHE_FILE, cache)


def _load_strdata(cache_key: str = "") -> str:
    d = _load_json(STRDATA_CACHE_FILE)
    if cache_key:
        entry = (d.get("accounts") or {}).get(cache_key) or {}
    else:
        entry = d
    saved_at = entry.get("saved_at", 0)
    if time.time() - saved_at < 7 * 86400:
        return entry.get("strData", "")
    return ""


def _save_strdata(strdata: str, cache_key: str = ""):
    d = _load_json(STRDATA_CACHE_FILE)
    if cache_key:
        accounts = d.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = d["accounts"] = {}
        accounts[cache_key] = {"strData": strdata, "saved_at": time.time()}
    else:
        d["strData"] = strdata
        d["saved_at"] = time.time()
    _write_json_secure(STRDATA_CACHE_FILE, d)


def refresh_ms_token_via_strdata(strdata: str = "", ms_appid: str = "6383",
                                  existing_ms_token: str = "", max_retries: int = 3,
                                  ua: str = "", cache_key: str = "") -> dict:
    """S0: 用 strData 重放 /web/r/token API 获取 msToken (纯 Python, 首选)。"""
    import requests as req

    if not strdata:
        strdata = _load_strdata(cache_key)
    if not strdata:
        return {"ok": False, "error": "no strData — 需要先从浏览器抓取一次 strData 指纹",
                "source": "strdata_replay"}

    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://www.douyin.com",
        "Referer": "https://www.douyin.com/",
        "User-Agent": ua or _UA,
    }
    body = dict(MSSDK_BODY_TEMPLATE)
    body["strData"] = strdata
    params = {"ms_appid": ms_appid}
    if existing_ms_token:
        params["msToken"] = existing_ms_token

    for attempt in range(max_retries):
        body["tspFromClient"] = int(time.time() * 1000)
        try:
            r = req.post(MSSDK_R_TOKEN_URL, params=params, headers=headers,
                         data=json.dumps(body, separators=(",", ":")), timeout=15)
            ms_token = r.headers.get("x-ms-token", "")
            if not ms_token:
                m = re.search(r"msToken=([^;]+)", r.headers.get("set-cookie", ""))
                if m:
                    ms_token = m.group(1)
            if ms_token:
                _save_strdata(strdata, cache_key)
                set_cached_ms_token(ms_token)
                return {"ok": True, "ms_token": ms_token, "source": "strdata_replay"}
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return {"ok": False, "error": f"x-ms-token not in response after {max_retries} retries (HTTP {r.status_code})",
                    "source": "strdata_replay"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return {"ok": False, "error": f"request error: {str(e)[:100]}", "source": "strdata_replay"}


def refresh_ms_token_via_mssdk(cookies_dict: dict, existing_ms_token: str = "",
                                str_data: str = "", ua: str = "") -> dict:
    """S2: 纯 Python mssdk API 两步交换 (需有效 cookies)。"""
    import requests as req

    safe_cookies = {k: v for k, v in cookies_dict.items() if "\r" not in v and "\n" not in v}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://creator.douyin.com",
        "Referer": "https://creator.douyin.com/",
        "User-Agent": ua or _UA,
    }

    body = dict(MSSDK_BODY_TEMPLATE)
    body["tspFromClient"] = int(time.time() * 1000)
    if str_data:
        body["strData"] = str_data

    params1 = {"ms_appid": "2906"}
    if existing_ms_token:
        params1["msToken"] = existing_ms_token

    r1 = req.post(MSSDK_R_TOKEN_URL, params=params1, headers=headers,
                  cookies=safe_cookies,
                  data=json.dumps(body, separators=(",", ":")), timeout=10)
    intermediate = r1.headers.get("x-ms-token", "")
    if not intermediate:
        return {"ok": False, "error": f"Step1: HTTP {r1.status_code} — x-ms-token not in response",
                "source": "mssdk_replay"}

    params2 = {"ms_appid": "2906", "msToken": intermediate}
    body2 = dict(MSSDK_BODY_TEMPLATE)
    body2["tspFromClient"] = int(time.time() * 1000)
    if str_data:
        body2["strData"] = str_data

    r2 = req.post(MSSDK_COMMON_URL, params=params2, headers=headers,
                  cookies=safe_cookies,
                  data=json.dumps(body2, separators=(",", ":")), timeout=10)
    final = r2.headers.get("x-ms-token", "")
    if not final:
        return {"ok": False, "error": f"Step2: HTTP {r2.status_code} — x-ms-token not in response",
                "source": "mssdk_replay"}

    set_cached_ms_token(final)
    return {"ok": True, "ms_token": final, "source": "mssdk_replay",
            "intermediate": intermediate}


def get_ms_token(cookies: dict = None, force_refresh: bool = False,
                 ms_appid: str = "2906", ua: str = "", identity=None) -> dict:
    """获取 msToken，自动选择最优策略。

    优先级: strData重放(纯Python) > 缓存(7天) > mssdk API > 远程签名服务
    ua 应传账号真实浏览器身份的 User-Agent,保证这里直连的 HTTP 请求
    和该账号浏览器实际发出的请求头一致。
    identity 非空时,远程 strData 会带账号画像(每账号不同指纹, 防共用被关联)。
    """
    ms_tok = (cookies or {}).get("msToken", "")
    profile = build_strdata_profile(ua, identity=identity)
    cache_key = _strdata_cache_key(ua, identity)

    result = refresh_ms_token_via_strdata(
        existing_ms_token=ms_tok, ms_appid=ms_appid, ua=ua, cache_key=cache_key)
    if result.get("ok"):
        return result

    if not force_refresh:
        cached = get_cached_ms_token()
        if cached:
            return {"ok": True, "ms_token": cached, "source": "cache"}

    if cookies:
        result = refresh_ms_token_via_mssdk(cookies, ms_tok, ua=ua)
        if result.get("ok"):
            return result

    # 模式B: 远程签名服务(strData 按账号画像参数化) — mssdk 重放必须本地 IP
    try:
        from .sign_client import remote_strdata
        strdata = remote_strdata(profile)
        if not strdata:
            strdata = _load_strdata(cache_key) or remote_strdata()
        if strdata:
            r = refresh_ms_token_via_strdata(
                strdata, ms_appid=ms_appid, ua=ua, cache_key=cache_key)
            if r.get("ok"):
                return r
    except Exception:
        pass

    return {"ok": False, "error": "all strategies failed — 需要先获取 strData", "source": "none"}
