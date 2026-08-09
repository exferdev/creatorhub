"""
抖音私信协议客户端 — 纯 HTTP Protobuf + WebSocket，无需浏览器。

参考:
  - Douyin_Spider (ccv-cat): WebSocket 心跳 + PushFrame 解码模式
  - E:\JS\project\douyin.com: protobuf 编解码 + imapi.douyin.com HTTP APIs

Protocol:
  - HTTP:   imapi.douyin.com (Content-Type: application/x-protobuf, 仅需 Cookie)
  - WebSocket: wss://frontier-im.douyin.com/ws/v2 (access_key + Cookie)
  - 认证:      Cookie (sessionid + sid_tt 等), 不需要 X-Bogus/a_bogus
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random as _random
import string as _string
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable
from urllib.parse import urlencode

# ═══════════════════════════════════════════════════════════════════
# Protobuf 编码工具
# ═══════════════════════════════════════════════════════════════════

def _pb_varint(value: int) -> bytes:
    result = []
    v = value
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        result.append(b)
        if v == 0: break
    return bytes(result)

def _pb_tag(field: int, wire: int) -> bytes:
    return _pb_varint((field << 3) | wire)

def _pb_string(field: int, s: str) -> bytes:
    data = s.encode("utf-8")
    return _pb_tag(field, 2) + _pb_varint(len(data)) + data

def _pb_varint_f(field: int, v: int) -> bytes:
    return _pb_tag(field, 0) + _pb_varint(v)

def _pb_bytes(field: int, data: bytes) -> bytes:
    return _pb_tag(field, 2) + _pb_varint(len(data)) + data

# ═══════════════════════════════════════════════════════════════════
# Protobuf 解码工具
# ═══════════════════════════════════════════════════════════════════

def _pb_parse_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = 0; shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0: break
        shift += 7
    return result, pos

def _pb_get_fields(raw: bytes) -> Dict[int, list]:
    out: Dict[int, list] = {}
    i = 0
    while i < len(raw):
        try:
            tag, i = _pb_parse_varint(raw, i)
        except (IndexError, Exception):
            break
        fn, wt = tag >> 3, tag & 7
        if fn == 0: break
        try:
            if wt == 0:
                v, i = _pb_parse_varint(raw, i)
            elif wt == 2:
                ln, i = _pb_parse_varint(raw, i); v = raw[i:i + ln]; i += ln
            else:
                break
        except (IndexError, Exception):
            break
        out.setdefault(fn, []).append(v)
    return out

def _pb_first(d: Dict[int, list], k: int, default=None):
    vv = d.get(k); return vv[0] if vv else default

def _pb_str(v) -> str:
    if isinstance(v, bytes):
        try: return v.decode("utf-8")
        except Exception: return ""
    return "" if v is None else str(v)

# ═══════════════════════════════════════════════════════════════════
# 请求构建 (与 DouyinIMAPI.build_request 完全一致)
# ═══════════════════════════════════════════════════════════════════

# Service IDs
SVC_INIT = 2043
SVC_HISTORY = 301
SVC_SEND = 100
SVC_MARK_READ = 2002
SVC_STRANGER_CONV = 1001

_FPID = "9"
_APP_KEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
_SALT = "f8a69f1719916z"

def _build_fingerprint(ua: str = "", platform: str = "Win32",
                       width: int = 1920, height: int = 1080,
                       device_id: str = "0") -> bytes:
    """field 15: repeated key-value pairs (浏览器指纹)"""
    ua_str = ua or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
    pairs = [
        ("session_aid", "6383"), ("session_did", "0"),
        ("app_name", "douyin_pc"), ("priority_region", "cn"),
        ("user_agent", ua_str), ("cookie_enabled", "true"),
        ("browser_language", "zh-CN"), ("browser_platform", platform),
        ("browser_name", "Mozilla"),
        ("browser_version", "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
        ("browser_online", "true"),
        ("screen_width", str(width)), ("screen_height", str(height)),
        ("referer", ""), ("timezone_name", "Asia/Shanghai"),
        ("deviceId", device_id), ("is-retry", "0"),
    ]
    res = b''
    for k, v in pairs:
        kv = _pb_string(1, k) + _pb_string(2, v)
        res += _pb_bytes(15, kv)
    return res


def _build_request(service_id: int, method_id: int, inner_body: bytes,
                   sdk_version: str = "0.1.8",
                   build_number: str = "0d50935:feat/pc-im-group",
                   ua: str = "", platform: str = "Win32",
                   device_id: str = "0", inner_field: int = None,
                   security_token: str = "", security_device_id: str = "") -> bytes:
    inner_f = inner_field if inner_field is not None else service_id
    body = b"".join([
        _pb_varint_f(1, service_id),
        _pb_varint_f(2, method_id),
        _pb_string(3, sdk_version),
        _pb_string(4, ""),
        _pb_varint_f(5, 3),
        _pb_varint_f(6, 1),
        _pb_string(7, build_number),
        _pb_bytes(8, _pb_bytes(inner_f, inner_body)),
        _pb_string(9, device_id),
        _pb_string(11, "douyin_pc"),
        _pb_string(14, "360000"),
        _build_fingerprint(ua, platform, device_id=device_id),
    ])
    # 附加 identity_security 字段 (浏览器 send 请求需要)
    if security_token:
        body += _pb_bytes(15, _pb_string(1, "identity_security_token") + _pb_string(2, security_token))
    if security_device_id:
        body += _pb_bytes(15,
            _pb_string(1, "identity_security_device_id") + _pb_string(2, security_device_id))
        body += _pb_bytes(15, _pb_string(1, "identity_security_aid") + _pb_string(2, ""))
    body += b"".join([
        _pb_varint_f(18, 1),
        _pb_string(21, "douyin_web"),
        _pb_string(22, "web_sdk"),
    ])
    return body


def _build_history_body(conv_id: str, conv_type: int = 1, conv_short_id: int = 0,
                         cursor: int = 0, count: int = 50) -> bytes:
    return b"".join([
        _pb_string(1, conv_id), _pb_varint_f(2, conv_type),
        _pb_varint_f(3, conv_short_id), _pb_varint_f(4, 1),
        _pb_varint_f(5, cursor), _pb_varint_f(6, count),
    ])


def _build_send_body(conv_id: str, text: str, conv_type: int = 1) -> bytes:
    """浏览器send格式 — 实测 200 OK(投递需完整安全token)"""
    msg_json = json.dumps({
        "aweType": 700, "type": 0, "richTextInfos": [], "text": text,
    }, ensure_ascii=False)
    ts_us = int(time.time() * 1_000_000)
    inner = b"".join([
        _pb_string(1, conv_id),
        _pb_varint_f(2, conv_type),
        _pb_varint_f(3, ts_us),
        _pb_string(4, msg_json),
    ])
    # 扩展字段 (s:mentioned_users, s:client_message_id, s:stime)
    mid = "".join(_random.choices(_string.hexdigits.lower(), k=8))
    mid += "-" + "".join(_random.choices(_string.hexdigits.lower(), k=4))
    mid += "-" + "".join(_random.choices(_string.hexdigits.lower(), k=4))
    mid += "-" + "".join(_random.choices(_string.hexdigits.lower(), k=4))
    mid += "-" + "".join(_random.choices(_string.hexdigits.lower(), k=12))
    for key, value in [
        ("s:mentioned_users", ""),
        ("s:client_message_id", mid),
        ("s:stime", str(int(time.time() * 1000)) + ".357"),
    ]:
        inner += _pb_bytes(5, _pb_string(1, key) + _pb_string(2, value))
    return inner


# ═══════════════════════════════════════════════════════════════════
# 消息解析
# ═══════════════════════════════════════════════════════════════════

def _ext_map(msg_fields: Dict[int, list]) -> Dict[str, str]:
    out = {}
    for e in msg_fields.get(9, []):
        if isinstance(e, bytes):
            kv = _pb_get_fields(e)
            k = _pb_str(_pb_first(kv, 1, b""))
            if k: out[k] = _pb_str(_pb_first(kv, 2, b""))
    return out


def _msg_create_ts(msg_fields: Dict[int, list]) -> int:
    ext = _ext_map(msg_fields)
    ms = ext.get("s:server_message_create_time") or ext.get("a:im_client_send_msg_time")
    if ms:
        try: return int(float(ms)) // 1000
        except Exception: pass
    v5 = _pb_first(msg_fields, 5)
    if isinstance(v5, int) and v5 > 1_000_000_000_000_000:
        return v5 // 1_000_000
    return 0


def _preview_text(content: bytes, msg_type=None) -> str:
    if not isinstance(content, bytes) or not content:
        return _MSG_LABEL.get(msg_type, "")
    try: obj = json.loads(content.decode("utf-8"))
    except Exception: return _MSG_LABEL.get(msg_type, "")
    if not isinstance(obj, dict): return _MSG_LABEL.get(msg_type, "")
    return str(obj.get("text") or obj.get("push_detail") or obj.get("description") or
               _MSG_LABEL.get(msg_type, ""))


def _parse_msg(msg_raw: bytes) -> dict:
    m = _pb_get_fields(msg_raw)
    content = _pb_first(m, 8, b"")
    content = content if isinstance(content, bytes) else b""
    msg_type = _pb_first(m, 6)
    direction = "in" if _pb_str(_pb_first(m, 7)) != _pb_str(_pb_first(m, 1)) else "out"
    return {
        "direction": direction,
        "text": _preview_text(content, msg_type),
        "msg_type": msg_type,
        "create_time": _msg_create_ts(m),
        "raw_json": content.decode("utf-8", "ignore") if content else "",
    }


def _peer_uid_from_conv_id(conv_id: str, self_uid: str) -> str:
    if not conv_id or not self_uid or ":" not in conv_id: return ""
    uids = [x for x in conv_id.split(":") if x.isdigit() and len(x) >= 6]
    return next((u for u in uids if u != self_uid), "")


def compute_access_key(device_id: str) -> str:
    return hashlib.md5((_FPID + _APP_KEY + str(device_id) + _SALT).encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# Passport 签名 + identity_security_token 获取
# ═══════════════════════════════════════════════════════════════════

def _derive_key_from_noon_utc(app_key: str) -> bytes:
    """HMAC-SHA256 PBKDF2-like 推导,种子为当天中午 UTC 时间戳。"""
    import hmac as _hmac
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    noon = datetime(now.year, now.month, now.day, 12, 0, 0, tzinfo=_tz.utc)
    noon_ts = str(int(noon.timestamp()))
    seed = noon_ts.encode()
    salt = app_key.encode()
    # HMAC 迭代
    result, intermediate, counter = b"", b"", 1
    while len(result) < 32:
        data = intermediate + salt + bytes([counter])
        intermediate = _hmac.new(seed, data, hashlib.sha256).digest()
        result += intermediate; counter += 1
    return result[:32]


def _passport_aid_sign(aid: str, path: str) -> str:
    """生成 passport API 签名头。"""
    import hmac as _hmac
    import base64 as _b64
    ts = str(int(time.time()))
    derived_key = _derive_key_from_noon_utc(_APP_KEY)
    sign_str = f"aid={aid}&path={path}&ts={ts}"
    sign_bytes = _hmac.new(derived_key, sign_str.encode(), hashlib.sha256).digest()
    sig = _b64.b64encode(sign_bytes).decode().rstrip("=")
    return ts, sig


def _fetch_identity_token(cookies: Dict[str, str], device_id: str, ua: str,
                          proxy: str = "") -> str:
    """调用 /passport/safe/get_identity_security_token 获取投递安全 token。"""
    import curl_cffi.requests as curl
    import base64 as _b64
    aid = "6383"
    # scene=web_im 是发送消息必需的场景标识(浏览器实测)
    path = "/passport/safe/get_identity_security_token"
    ts, sig = _passport_aid_sign(aid, path)
    url = (f"https://www.douyin.com{path}?passport_jssdk_version=4.2.3"
           f"&passport_jssdk_type=lite&is_from_ttaccountsdk=1&aid={aid}"
           f"&language=zh&scene=web_im&auto_retry_req=0&skip_verify=false"
           f"&identity_token_force_get_tag=0&device_platform=web_app&ts={ts}")
    h = {
        "User-Agent": ua, "Referer": "https://www.douyin.com/",
        "x-passport-request-sign": f"ts={ts},sign={sig}",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json, text/plain, */*",
    }
    sess = curl.Session()
    sess.headers.update(h)
    try:
        resp = sess.get(url, cookies=cookies, timeout=10)
        data = resp.json()
        token = (data.get("data") or {}).get("identity_security_token") or ""
        if token and isinstance(token, str) and len(token) > 10:
            return json.dumps({"token": token})
    except Exception:
        pass
    return ""


_MSG_LABEL = {1: "[文本]", 2: "[图片]", 3: "[视频]", 4: "[语音]", 5: "[表情]",
              6: "[链接]", 7: "[文件]", 8: "[卡片]", 9: "[红包]"}


# ═══════════════════════════════════════════════════════════════════
# WebSocket 帧处理 (Douyin_Spider 模式)
# ═══════════════════════════════════════════════════════════════════

def _decode_ws_push(raw: bytes) -> Optional[dict]:
    """解析 PushFrame → Payload gzip → Response → field 500 NewMessageNotify"""
    try:
        frame = _pb_get_fields(raw)
        payload = _pb_first(frame, 8)  # field 8 = gzip'd protobuf
        if not payload or not isinstance(payload, bytes):
            return None
        decompressed = gzip.decompress(payload)
        resp = _pb_get_fields(decompressed)
        # field 500 = NewMessageNotify
        notify = _pb_first(resp, 500)
        if not notify or not isinstance(notify, bytes):
            return None
        msg_body = _pb_get_fields(notify)
        conv_id = _pb_str(_pb_first(msg_body, 1))
        sender_uid = _pb_str(_pb_first(msg_body, 2))
        msg_type = _pb_first(msg_body, 4)
        content_raw = _pb_first(msg_body, 5)
        if isinstance(content_raw, bytes):
            try: content = content_raw.decode("utf-8")
            except Exception: content = ""
        else: content = ""
        return {
            "conv_id": conv_id, "sender_uid": sender_uid,
            "msg_type": msg_type, "content": content,
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# DouyinIMClient — 协议 IM 客户端
# ═══════════════════════════════════════════════════════════════════

class DouyinIMClient:
    """抖音私信协议客户端。

    提供方式：
      - HTTP APIs: 会话列表 / 消息历史 / 发送 / 标记已读
      - WebSocket: 实时推送 (参考 Douyin_Spider WebSocket 模式)

    使用账号的浏览器 Profile 信息 (device_id, cookies, UA, fpid) 构造请求。
    """

    API_BASE = "https://imapi.douyin.com"
    WS_BASE = "wss://frontier-im.douyin.com/ws/v2"

    def __init__(self, cookies: Dict[str, str], device_id: str,
                 ua: str = "", platform: str = "Win32",
                 proxy: str = "", fpid: str = _FPID):
        self.cookies = dict(cookies)
        self.device_id = str(device_id)
        self.ua = ua or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
        self.platform = platform
        self.fpid = fpid
        self.self_uid = ""   # 在 get_message_by_init 后自动填充
        self._method_id = 10356
        # HTTP session
        import curl_cffi.requests as curl
        self._sess = curl.Session()
        self._sess.headers.update({
            "Content-Type": "application/x-protobuf",
            "Accept": "application/x-protobuf",
            "User-Agent": self.ua,
            "Referer": "https://www.douyin.com/",
            "Origin": "https://www.douyin.com",
        })
        if proxy:
            self._sess.proxies = {"http": proxy, "https": proxy}

    @classmethod
    def from_account(cls, account, proxy: str = "") -> "DouyinIMClient":
        """从 DouyinAccount 对象构造客户端。"""
        import json as _json
        state = account.storage_state or account.creator_storage_state or ""
        cookies = {}
        try:
            st = _json.loads(state)
            for c in st.get("cookies", []):
                n, v = c.get("name", ""), c.get("value", "")
                if n: cookies[n] = v
        except Exception:
            pass
        # 从 cookie 或其他来源提取 device_id
        # 优先用数字 douyin_id, fallback 到 uid_tt cookie
        device_id = account.douyin_id or cookies.get("uid_tt", "") or "0"
        ua = getattr(account, "ua", "") or ""
        plat = "MacIntel" if "Mac" in ua else "Win32"
        return cls(cookies, device_id, ua=ua, platform=plat, proxy=proxy)

    # ── access_key / WS URL ──

    @property
    def access_key(self) -> str:
        return hashlib.md5(
            (self.fpid + _APP_KEY + self.device_id + _SALT).encode()
        ).hexdigest()

    @property
    def ws_url(self) -> str:
        did = self.self_uid or self.device_id  # self_uid 是真正的数字 UID
        params = {
            "aid": "6383", "fpid": self.fpid,
            "device_id": did, "access_key": self.access_key,
            "device_platform": "douyin_pc", "version_code": "360000",
        }
        return f"{self.WS_BASE}?{urlencode(params)}"

    # ── 内部方法 ──

    def _next_mid(self) -> int:
        self._method_id += 1; return self._method_id

    def _gen_abogus(self, path: str, query_params: dict = None) -> str:
        """用 V8 signer.generate_a_bogus() 生成 a_bogus (需要 V8 运行时初始化)。"""
        try:
            from . import signer as _signer
            if not (_signer._ready and _signer._signer):
                return ""
            from urllib.parse import urlencode
            qs = ""
            if query_params:
                qs = urlencode({k: v for k, v in query_params.items() if v})
            url = f"https://imapi.douyin.com{path}"
            if qs:
                url += "?" + qs
            cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            return _signer._signer.generate_a_bogus(
                url=url, method="POST",
                cookies=cookie_str, ua=self.ua,
                debug=False) or ""
        except Exception:
            pass
        return ""

    def _post(self, path: str, body: bytes, query_params: dict = None,
              a_bogus: str = "") -> bytes:
        import curl_cffi.requests as curl
        from urllib.parse import urlencode
        url = f"{self.API_BASE}{path}"
        params = dict(query_params or {})
        if a_bogus:
            params["a_bogus"] = a_bogus
        if params:
            qs = urlencode({k: v for k, v in params.items() if v})
            url += "?" + qs
        try:
            resp = self._sess.post(url, data=body, cookies=self.cookies, timeout=30)
            print(f"[im-protocol] POST {path} -> {resp.status_code}, {len(resp.content)} bytes")
            return resp.content
        except Exception as e:
            print(f"[im-protocol] POST {path} ERROR: {e!r}")
            return b""
            return b""

    def _make_request(self, service_id: int, inner_body: bytes,
                       inner_field: int = None,
                       security_token: str = "", security_device_id: str = "") -> bytes:
        return _build_request(service_id, self._next_mid(), inner_body,
                              ua=self.ua, platform=self.platform,
                              device_id=self.device_id,
                              inner_field=inner_field,
                              security_token=security_token,
                              security_device_id=security_device_id)

    # ── get_message_by_init: 初始化会话列表 ──

    def get_message_by_init(self) -> List[dict]:
        """返回会话列表 (与浏览器 get_message_by_init XHR 等效)。"""
        inner = _pb_varint_f(2, 0)
        body = self._make_request(SVC_INIT, inner)
        raw = self._post("/v1/message/get_message_by_init", body)
        return self._parse_init(raw)

    def _parse_init(self, raw: bytes) -> List[dict]:
        if not raw: return []
        env = _pb_get_fields(raw)
        self.self_uid = _pb_str(_pb_first(env, 13))
        body_raw = _pb_first(env, 6)
        if not isinstance(body_raw, bytes): return []
        body = _pb_get_fields(body_raw)
        b2043 = _pb_first(body, 2043)
        if not isinstance(b2043, bytes): return []
        bf = _pb_get_fields(b2043)
        results = []
        for cb in bf.get(1, []):
            if not isinstance(cb, bytes): continue
            conv = _pb_get_fields(cb)
            core = _pb_get_fields(_pb_first(conv, 1, b"") or b"")
            conv_id = _pb_str(_pb_first(core, 1, b""))
            if not conv_id: continue
            ctype = _pb_first(core, 3)
            if ctype != 1: continue
            conv_short_id = _pb_str(_pb_first(core, 2))
            ticket = _pb_str(_pb_first(core, 4, b""))
            peer_uid = _peer_uid_from_conv_id(conv_id, self.self_uid)
            if not peer_uid: continue

            # 会话包含的消息 (get_message_by_init 自带)
            messages_raw = [m for m in conv.get(2, []) if isinstance(m, bytes)]
            init_messages = [_parse_msg(m) for m in messages_raw]
            init_messages = [m for m in init_messages if m]

            # 最后一条消息
            last_msg_raw = messages_raw[-1] if messages_raw else None
            if last_msg_raw:
                msg = _pb_get_fields(last_msg_raw)
                last_content = _pb_first(msg, 8, b"")
                last_content = last_content if isinstance(last_content, bytes) else b""
                last_type = _pb_first(msg, 6)
                last_ts = _msg_create_ts(msg)
            else:
                last_content, last_type, last_ts = b"", None, 0

            peer_sec = ""
            for p in core.get(6, []):
                if not isinstance(p, bytes): continue
                for pp in _pb_get_fields(p).get(1, []):
                    if isinstance(pp, bytes):
                        ppf = _pb_get_fields(pp)
                        if _pb_str(_pb_first(ppf, 1)) == peer_uid:
                            peer_sec = _pb_str(_pb_first(ppf, 5)); break
                if peer_sec: break
            if not peer_sec and last_msg_raw:
                lm = _pb_get_fields(last_msg_raw)
                if _pb_first(lm, 7) != int(self.self_uid or 0):
                    peer_sec = _pb_str(_pb_first(lm, 14, b""))

            cursor = _pb_first(conv, 4) or 0
            results.append({
                "conv_id": conv_id, "conv_short_id": conv_short_id,
                "peer_uid": peer_uid, "peer_sec_uid": peer_sec,
                "ticket": ticket,
                "last_text": _preview_text(last_content, last_type),
                "last_msg_type": last_type,
                "last_time": last_ts,
                "cursor": cursor,
                "messages": init_messages,     # ← 新增:get_message_by_init返回中自带的初始消息
            })
        return results

    # ── get_by_conversation: 会话消息历史 ──

    def get_by_conversation(self, conv_id: str, conv_type: int = 1,
                             conv_short_id: int = 0, cursor: int = 0,
                             count: int = 50) -> Tuple[List[dict], bool, int]:
        inner = _build_history_body(conv_id, conv_type, conv_short_id, cursor, count)
        body = self._make_request(SVC_HISTORY, inner)
        raw = self._post("/v1/message/get_by_conversation", body)
        print(f"[im-protocol] get_by_conversation raw_len={len(raw)} preview={raw[:200]!r}")
        return self._parse_history(raw)

    def _parse_history(self, raw: bytes) -> Tuple[List[dict], bool, int]:
        if not raw: return [], False, 0
        env = _pb_get_fields(raw)
        b301 = _pb_first(env, 6)
        if not isinstance(b301, bytes): return [], False, 0
        body = _pb_get_fields(b301)
        b301_inner = _pb_first(body, 301)
        if not isinstance(b301_inner, bytes): return [], False, 0
        inner = _pb_get_fields(b301_inner)
        msgs = [_parse_msg(m) for m in inner.get(1, []) if isinstance(m, bytes)]
        next_cursor = _pb_first(body, 5)
        has_more = _pb_first(body, 4) == 1
        nc = next_cursor if isinstance(next_cursor, int) else 0
        return msgs, has_more, nc

    # ── send_message: 发送 ──

    def send_message(self, conv_id: str, text: str, conv_type: int = 1) -> dict:
        inner = _build_send_body(conv_id, text, conv_type)
        # 获取投递必需的 identity_security_token (scene=web_im)
        security_token = ""
        security_device_id = str(self.self_uid or self.device_id)
        try:
            security_token = _fetch_identity_token(
                self.cookies, security_device_id, self.ua)
        except Exception as e:
            print(f"[im-protocol] identity_token fetch fail: {e!r}")
        body = self._make_request(SVC_SEND, inner,
                                  security_token=security_token,
                                  security_device_id=security_device_id)
        # 构建带签名参数的 URL (浏览器实测需要 a_bogus+msToken+verifyFp)
        ms_token = self.cookies.get("msToken", "")
        verify_fp = self.cookies.get("UIFID_TEMP", "")[:19] or ""
        url_path = "/v1/message/send"
        query_params = {"msToken": ms_token or ""}
        if verify_fp and len(verify_fp) > 10:
            query_params["verifyFp"] = query_params["fp"] = f"verify_{verify_fp}"
        # 用 V8 签名器生成 a_bogus
        a_bogus = ""
        try:
            a_bogus = self._gen_abogus(url_path, query_params)
            print(f"[im-protocol] a_bogus={'OK' if a_bogus else 'FAIL'}")
        except Exception as e:
            print(f"[im-protocol] a_bogus error: {e!r}")
        raw = self._post(url_path, body, query_params=query_params, a_bogus=a_bogus)
        print(f"[im-protocol] send_message raw={len(raw)}b")
        if not raw: return {"ok": False, "msg": "empty", "cmd": 0}
        env = _pb_get_fields(raw)
        msg = _pb_str(_pb_first(env, 4, b""))
        cmd = _pb_first(env, 1) or 0
        err = _pb_first(env, 3) or 0
        return {"ok": (msg == "OK"), "msg": msg, "cmd": cmd, "error_code": err}

    # ── mark_read: 标记已读 ──

    def mark_read(self, conv_id: str) -> bool:
        inner = _pb_string(1, conv_id) + _pb_varint_f(2, 0) + _pb_varint_f(3, 0)
        body = self._make_request(SVC_MARK_READ, inner, inner_field=604)
        raw = self._post("/v3/conversation/mark_read", body)
        if not raw: return False
        env = _pb_get_fields(raw)
        return _pb_first(env, 3) == 0

    # ── WebSocket 实时接收 (Douyin_Spider 模式) ──

    async def ws_connect(self) -> None:
        """连接 frontier-im WebSocket。"""
        import websockets
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                subprotocols=["binary", "base64", "pbbp2"],
                additional_headers={"User-Agent": self.ua},
                ping_interval=None,
                max_size=2 ** 22,
            )
        except ImportError:
            self._ws = None
            raise RuntimeError("websockets 库未安装: pip install websockets")

    async def ws_heartbeat(self, stop_event: asyncio.Event):
        """每 15s 发送 hi 文本帧 (与 Douyin_Spider PushFrame hb 等价)。"""
        while not stop_event.is_set():
            await asyncio.sleep(15)
            try:
                if self._ws:
                    await self._ws.send("hi")
            except Exception:
                break

    async def ws_listen(self, on_message: Callable[[dict], None],
                        stop_event: asyncio.Event):
        """接收 WebSocket 二进制帧,解析 PushFrame → field 500 新消息通知。"""
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            except (asyncio.TimeoutError, Exception):
                continue
            if not isinstance(raw, bytes) or raw == b"":
                continue
            msg = _decode_ws_push(raw)
            if msg:
                on_message(msg)

    async def ws_close(self):
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
