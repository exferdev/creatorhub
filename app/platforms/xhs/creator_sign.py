"""小红书签名：从 js-sign-service (Cloudflare Worker) 远程获取。

服务地址: https://js.faryi.workers.dev (GitHub: https://github.com/exferdev/js)
路由:     POST /sign/xhs/{all|x_rap|cos_sign|traceid|xs|xs_common|xyw}

- x-s / x-t / x-s-common / b3/xray traceid: 用 `/sign/xhs/all` 一次聚合
- x-rap-param: 用 `/sign/xhs/x_rap`
- 上传 COS 签名: 用 `/sign/xhs/cos_sign`
- traceid: 用 `/sign/xhs/traceid`

彻底远程化：不再打包签名 JS、不依赖 Node/execjs/crypto-js。
非签名辅助(纯 Python)保留本地：splice_str / trans_cookies / now_ms。
远程签名服务可用 `SIGN_SERVICE_URL` 环境变量覆盖(与 douyin sign_client 一致)。
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlencode

_BASE_URL = os.environ.get("SIGN_SERVICE_URL", "https://js.faryi.workers.dev")
_TIMEOUT = 8

_AVAILABLE: bool | None = None
_AVAILABLE_AT = 0.0
_AVAILABLE_TTL = 60.0


def _post(algorithm: str, payload: dict) -> dict:
    """调远程签名服务；非 ok 响应抛 RuntimeError。"""
    import curl_cffi.requests as cr
    url = f"{_BASE_URL}/sign/xhs/{algorithm}"
    resp = cr.post(url, json=payload, timeout=_TIMEOUT)
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(
            f"sign service error: {data.get('error', resp.status_code)}")
    return data


def available() -> bool:
    """探测远程签名服务可达性(轻量 traceid, 60s 缓存)。"""
    global _AVAILABLE, _AVAILABLE_AT
    now = time.time()
    if _AVAILABLE is not None and now - _AVAILABLE_AT < _AVAILABLE_TTL:
        return _AVAILABLE
    try:
        _post("traceid", {})
        _AVAILABLE, _AVAILABLE_AT = True, now
    except Exception:
        _AVAILABLE, _AVAILABLE_AT = False, now
    return bool(_AVAILABLE)


def generate_xs_xs_common(a1: str, api: str, data="") -> tuple:
    """x-s / x-t / x-s-common 一次聚合(all)。"""
    ret = _post("all", {"a1": a1, "api": api, "method": "GET", "data": data})
    return ret["x_s"], ret["x_t"], ret["x_s_common"]


def generate_xsc_main(a1: str, api: str, data="", method: str = "GET") -> dict:
    """网页主签名(x-s/x-t/x-s-common + traceid)。api 传"路径?query"。"""
    ret = _post("all", {"a1": a1, "api": api, "method": method, "data": data})
    return {
        "x-s": ret["x_s"], "x-t": str(ret["x_t"]), "x-s-common": ret["x_s_common"],
        "x-b3-traceid": ret["x_b3_traceid"], "x-xray-traceid": ret["x_xray_traceid"],
    }


def generate_xsc(a1: str, api: str, data="") -> dict:
    ret = _post("all", {"a1": a1, "api": api, "method": "GET", "data": data})
    return {
        "x-s": ret["x_s"], "x-t": str(ret["x_t"]), "x-s-common": ret["x_s_common"],
        "x-b3-traceid": ret["x_b3_traceid"], "x-xray-traceid": ret["x_xray_traceid"],
    }


def generate_x_rap_param(api: str, data="") -> str:
    if isinstance(data, (dict, list)):
        import json
        data = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    ret = _post("x_rap", {"api": api, "data": data})
    return ret["x_rap_param"]


def cos_signature(message: str, file_id: str, content_length: int,
                  host: str = "ros-upload.xiaohongshu.com") -> str:
    """上传 COS 签名(腾讯云风格 HMAC-SHA1)，由 worker cos_sign 计算。"""
    ret = _post("cos_sign", {
        "message": message, "file_id": file_id,
        "content_length": content_length, "host": host,
    })
    return ret["signature"]


def gen_b3_traceid() -> str:
    return _post("traceid", {})["x_b3_traceid"]


def gen_xray_traceid() -> str:
    return _post("traceid", {})["x_xray_traceid"]


def splice_str(api: str, params: dict) -> str:
    return api + "?" + urlencode(
        {k: ("" if v is None else v) for k, v in params.items()}, doseq=True)


def trans_cookies(cookie_str: str) -> dict:
    sep = "; " if "; " in cookie_str else ";"
    out = {}
    for part in cookie_str.split(sep):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
    return out


# 13 位时间戳辅助
def now_ms() -> int:
    return int(time.time() * 1000)
