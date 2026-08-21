"""
远程签名服务客户端 — 调用 exferdev/js (Cloudflare Worker)

服务地址: https://js.faryi.workers.dev (GitHub: https://github.com/exferdev/js)
路由: POST /sign/:platform/:algorithm

完全云端签名: 失败返回 None, 由调用方决定抛错或跳过(无本地回退)。

网络抖动/Worker 冷启动场景: _post 自动重试 3 次(1s/2s 递增退避), 超时 10s。
最近一次失败的**明细**(超时/非 JSON/服务端错误)记入 last_error(), 调用方报错
时带上它, 让异机上的失败(如 workers.dev 不可达)一眼可查, 而不是笼统的"未返回"。
"""
import os
import time
from typing import Optional

_BASE_URL = os.environ.get("SIGN_SERVICE_URL", "https://js.faryi.workers.dev")
_TIMEOUT = 10          # 单次请求超时(秒); Worker 冷启动可达数秒
_MAX_ATTEMPTS = 3      # 最大尝试次数(含首试)

_LAST_ERROR = ""       # 最近一次失败明细(空=上次调用成功)


class SignServiceError(RuntimeError):
    """远程签名服务调用失败(网络/协议/服务端错误, 含明细)。"""


def last_error() -> str:
    """最近一次签名调用的失败明细, 供报错信息把根因带给用户与日志。"""
    return _LAST_ERROR


def _post(algorithm: str, payload: dict) -> dict:
    """调用远程签名服务; 失败重试 _MAX_ATTEMPTS 次, 最终抛 SignServiceError。
    每次失败都会更新 _LAST_ERROR 为可读明细。"""
    global _LAST_ERROR
    import curl_cffi.requests as cr
    url = f"{_BASE_URL}/sign/douyin/{algorithm}"
    detail = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = cr.post(url, json=payload, timeout=_TIMEOUT)
            data = resp.json()
            if not data.get("ok"):
                detail = f"服务返回错误: {data.get('error', resp.status_code)}"
                raise SignServiceError(detail)
            _LAST_ERROR = ""
            return data
        except SignServiceError:
            if attempt < _MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            _LAST_ERROR = detail
            raise
        except Exception as e:
            status = ""
            try:
                status = f" HTTP {getattr(e.response, 'status_code', '')}"
            except Exception:
                pass
            detail = (f"请求失败 {type(e).__name__}{status}: "
                      f"{str(e)[:120]} (url={url})")
            if attempt < _MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            _LAST_ERROR = detail
            raise SignServiceError(detail) from e


def remote_abogus(params: str, ua: str = "") -> Optional[str]:
    """远程生成 a_bogus。失败返回 None (明细见 last_error())。"""
    try:
        data = _post("abogus", {"params": params, "ua": ua or None})
        return data.get("a_bogus")
    except Exception:
        return None


def remote_xbogus(path: str, salt: str = "") -> Optional[str]:
    """远程生成 X-Bogus。失败返回 None (明细见 last_error())。"""
    try:
        data = _post("xbogus", {"path": path, "salt": salt})
        return data.get("x_bogus")
    except Exception:
        return None


def remote_strdata(profile: dict | None = None) -> Optional[str]:
    """远程生成 strData 指纹(msToken 重放用), 可传账号画像参数使每账号指纹不同。

    worker strdata 端点白名单: ua/platform/deviceMemory/hardwareConcurrency/
    language/languages/timezone/canvas/browserType/screen{width,height,colorDepth}/
    viewport_w/h。失败返回 None (明细见 last_error())。
    """
    try:
        data = _post("strdata", profile or {})
        return data.get("strData")
    except Exception:
        return None


def remote_mstoken(strdata: str = "", ms_appid: str = "6383", ua: str = "") -> Optional[str]:
    """远程 strData 重放获取 msToken。失败返回 None (明细见 last_error())。"""
    try:
        payload = {"ms_appid": ms_appid, "ua": ua or None}
        if strdata:
            payload["strData"] = strdata
        data = _post("mstoken", payload)
        return data.get("ms_token")
    except Exception:
        return None