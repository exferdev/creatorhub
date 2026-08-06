"""Cookie 健康探活模块 — 参考 douyin-ops account_health.py。

双层验证:
  Phase 1: creator.douyin.com/account/api/v1/user/account/info — 基础会话
  Phase 2: creator.douyin.com/aweme/mid/video/sts2/         — 发布权限

用 curl_cffi（项目已有依赖）发纯 HTTP 探活请求，不启浏览器。
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlencode

from ..browser.identity import _platform_bits

CREATOR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _browser_platform(ua: str) -> str:
    if not ua:
        return "Win32"
    return _platform_bits(ua)[0]


def _parse_storage_state(storage_state_json: str) -> dict:
    """从 Playwright storage_state JSON 中提取 cookie 字典。"""
    cookies = {}
    if not storage_state_json:
        return cookies
    try:
        state = json.loads(storage_state_json)
        for c in state.get("cookies", []):
            name = c.get("name", "")
            if name:
                cookies[name] = c.get("value", "")
    except Exception:
        pass
    return cookies


def _cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _csrf_token(cookies: dict) -> str:
    return cookies.get("passport_csrf_token", "")


def check_creator_web_session(storage_state_json: str, ua: str = "") -> dict:
    """Phase 1: 验证创作者平台基础会话是否有效。

    ua 应传该账号真实浏览器身份的 User-Agent(见 browser.identity.Identity.ua),
    保证这里的纯 HTTP 探活请求头和账号平时用的浏览器身份一致。
    返回 {"valid": True} 或 {"valid": False, "error": "..."}。
    """
    from curl_cffi import requests as curl_req

    cookies = _parse_storage_state(storage_state_json)
    if not cookies.get("sessionid"):
        return {"valid": False, "error": "缺少 sessionid"}

    url = "https://creator.douyin.com/account/api/v1/user/account/info"
    headers = {
        "User-Agent": ua or CREATOR_UA,
        "Referer": "https://creator.douyin.com/creator-micro/home",
        "Accept": "application/json, text/plain, */*",
        "Cookie": _cookie_header(cookies),
    }
    csrf = _csrf_token(cookies)
    if csrf:
        headers["x-secsdk-csrf-token"] = csrf

    try:
        resp = curl_req.get(url, headers=headers, impersonate="chrome131", timeout=15)
    except Exception as e:
        return {"valid": False, "error": f"请求异常: {type(e).__name__}: {e!r}"}

    try:
        data = resp.json()
    except Exception:
        return {"valid": False,
                "error": f"非 JSON 响应 (HTTP {resp.status_code})",
                "body": (resp.text or "")[:200]}

    if resp.status_code == 200 and data.get("status_code") == 0 and data.get("account_info") is not None:
        return {"valid": True}

    return {"valid": False,
            "error": f"HTTP {resp.status_code} status_code={data.get('status_code')}",
            "detail": json.dumps(data, ensure_ascii=False)[:300]}


def check_creator_publish_session(storage_state_json: str, ua: str = "") -> dict:
    """Phase 2: 验证创作者平台发布权限是否有效。

    先跑 Phase 1，再调 sts2 接口看是否拿到发布凭证。
    """
    from curl_cffi import requests as curl_req

    web = check_creator_web_session(storage_state_json, ua=ua)
    if not web["valid"]:
        return web

    cookies = _parse_storage_state(storage_state_json)
    params = {
        "scene": "web", "aid": "2906", "app_name": "aweme_creator_platform",
        "device_platform": "web", "cookie_enabled": "true",
        "browser_language": "zh-CN", "browser_platform": _browser_platform(ua),
        "support_h265": "1",
    }
    url = f"https://creator.douyin.com/aweme/mid/video/sts2/?{urlencode(params)}"
    headers = {
        "User-Agent": ua or CREATOR_UA,
        "Referer": "https://creator.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": _cookie_header(cookies),
    }

    try:
        resp = curl_req.get(url, headers=headers, impersonate="chrome131", timeout=15)
    except Exception as e:
        return {"valid": False, "error": f"STS2 请求异常: {type(e).__name__}: {e!r}"}

    try:
        data = resp.json()
    except Exception:
        return {"valid": False,
                "error": f"STS2 非 JSON 响应 (HTTP {resp.status_code})",
                "body": (resp.text or "")[:200]}

    if resp.status_code != 200:
        return {"valid": False,
                "error": f"STS2 HTTP {resp.status_code}",
                "detail": json.dumps(data, ensure_ascii=False)[:300]}

    missing = [k for k in ("access_key_id", "secret_access_key", "session_token") if not data.get(k)]
    if data.get("status_code") == 0 and not missing:
        return {"valid": True}

    return {"valid": False,
            "error": f"STS2 status_code={data.get('status_code')}, missing={missing}",
            "detail": json.dumps(data, ensure_ascii=False)[:300]}


def check_cookie_health(storage_state_json: str, publish_required: bool = True, ua: str = "") -> dict:
    """统一入口:根据是否要发布选择检测深度。

    publish_required=True → Phase 1 + Phase 2
    publish_required=False → Phase 1 only
    """
    if publish_required:
        return check_creator_publish_session(storage_state_json, ua=ua)
    return check_creator_web_session(storage_state_json, ua=ua)
