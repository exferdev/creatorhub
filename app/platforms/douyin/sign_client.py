"""
远程签名服务客户端 — 调用 exferdev/js (Cloudflare Worker)

服务地址: https://js.faryi.workers.dev (GitHub: https://github.com/exferdev/js)
路由: POST /sign/:platform/:algorithm

完全云端签名: 失败返回 None, 由调用方决定抛错或跳过(无本地回退)。
"""
import os
from typing import Optional

_BASE_URL = os.environ.get("SIGN_SERVICE_URL", "https://js.faryi.workers.dev")
_TIMEOUT = 8


def _post(algorithm: str, payload: dict) -> dict:
    import curl_cffi.requests as cr
    url = f"{_BASE_URL}/sign/douyin/{algorithm}"
    resp = cr.post(url, json=payload, timeout=_TIMEOUT)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"sign service error: {data.get('error', resp.status_code)}")
    return data


def remote_abogus(params: str, ua: str = "") -> Optional[str]:
    """远程生成 a_bogus。失败返回 None。"""
    try:
        data = _post("abogus", {"params": params, "ua": ua or None})
        return data.get("a_bogus")
    except Exception:
        return None


def remote_xbogus(path: str, salt: str = "") -> Optional[str]:
    """远程生成 X-Bogus。失败返回 None。"""
    try:
        data = _post("xbogus", {"path": path, "salt": salt})
        return data.get("x_bogus")
    except Exception:
        return None


def remote_strdata() -> Optional[str]:
    """远程生成 strData 指纹。失败返回 None。"""
    try:
        data = _post("strdata", {})
        return data.get("strData")
    except Exception:
        return None


def remote_mstoken(strdata: str = "", ms_appid: str = "6383", ua: str = "") -> Optional[str]:
    """远程 strData 重放获取 msToken。失败返回 None。"""
    try:
        payload = {"ms_appid": ms_appid, "ua": ua or None}
        if strdata:
            payload["strData"] = strdata
        data = _post("mstoken", payload)
        return data.get("ms_token")
    except Exception:
        return None
