"""抖音协议发布 — 8 步流程通过浏览器网络栈发包。

基于 douyin-ops publisher.py 的协议逆向，但 HTTP 层改用 page.evaluate(fetch)
走真实浏览器 TLS 指纹（继承 CreatorHub 反检测体系），而非 curl_cffi 模拟。

签名（X-Bogus / a_bogus）由远程签名服务 js-sign-service 提供（SIGN_SERVICE_URL,
github.com/exferdev/js）。msToken 由 ms_token.py 提供。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import binascii
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests as req

from ...browser.identity import Identity, _platform_bits
from ...browser.manager import BrowserManager
from . import ms_token as _ms_token

logger = logging.getLogger(__name__)

UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COMMON_PARAMS = dict(
    aid="2906", app_name="aweme_creator_platform", device_platform="web",
    cookie_enabled="true", screen_width="1920", screen_height="1080",
    browser_language="zh-CN", browser_platform="Win32",
)


def _browser_platform(ua: str) -> str:
    """由账号真实 UA 推断 browser_platform 业务参数，与 identity.py 的指纹注入逻辑保持一致，
    避免协议请求里的 browser_platform / 签名内容和浏览器真实身份（尤其是 Mac 指纹账号）不一致。"""
    if not ua:
        return "Win32"
    return _platform_bits(ua)[0]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _crc32_hex(data: bytes) -> str:
    return format(binascii.crc32(data) & 0xFFFFFFFF, "08x")


def _csrf_token(cookies: dict) -> str:
    return cookies.get("passport_csrf_token", "")


def _cookie_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _parse_content_tags(tags: str) -> list[str]:
    seen = set()
    result = []
    for raw_tag in re.split(r"[,，;；\s]+", tags or ""):
        tag = raw_tag.strip().lstrip("#＃").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def _build_publish_text(title: str, description: str, tags: str) -> tuple[str, list[str]]:
    tag_list = _parse_content_tags(tags)
    text_parts = [part for part in ((title or "").strip(), (description or "").strip())
                  if part]
    tag_text = " ".join(f"#{tag}" for tag in tag_list)
    if tag_text:
        text_parts.append(tag_text)
    return " ".join(text_parts) if text_parts else (title or "").strip(), tag_list


def _sanitize_publish_title(title: str, limit: int = 30) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    chars = []
    for ch in normalized:
        category = unicodedata.category(ch)
        if category[0] in ("L", "N") or ch.isspace():
            chars.append(ch)
    safe_title = re.sub(r"\s+", " ", "".join(chars)).strip()
    return safe_title[:limit] or "作品"


def _build_hashtag_metadata(full_text: str, caption: str, tags: list[str]) -> tuple[str, str]:
    extras = []
    challenges = []
    for tag in tags:
        marker = f"#{tag}"
        start = full_text.find(marker)
        if start < 0:
            continue
        item = {
            "start": start, "end": start + len(marker),
            "type": 1, "hashtag_name": tag, "hashtag_id": 0, "user_id": "",
        }
        extras.append(item)
    return (
        json.dumps(extras, ensure_ascii=False, separators=(",", ":")),
        json.dumps(challenges, separators=(",", ":")),
    )


# ═══════════════════════════════════════════════════════════════════
# 浏览器 HTTP 发送层
# ═══════════════════════════════════════════════════════════════════

async def _browser_get(page, path: str, params: dict = None,
                       headers: dict = None, extra_headers: dict = None) -> dict:
    """通过浏览器 fetch() 发送 GET，返回 {status, body_text}。"""
    query = urlencode(params) if params else ""
    url = f"https://creator.douyin.com{path}"
    if query:
        url += "?" + query
    hdrs = dict(headers or {})
    hdrs.update(extra_headers or {})
    if "Referer" not in hdrs:
        hdrs["Referer"] = "https://creator.douyin.com/"
    js = f"""(async () => {{
        const r = await fetch({json.dumps(url)}, {{method:'GET',headers:{json.dumps(hdrs)},credentials:'include'}});
        return {{status:r.status, body:await r.text()}};
    }})()"""
    try:
        result = await page.evaluate(js)
        return result
    except Exception as e:
        return {"status": -1, "body": f"page.evaluate error: {e!r}"}


async def _browser_post(page, path: str, params: dict = None,
                        body: dict = None, headers: dict = None,
                        extra_headers: dict = None) -> dict:
    """通过浏览器 fetch() 发送 POST JSON，返回 {status, body_text}。"""
    query = urlencode(params) if params else ""
    url = f"https://creator.douyin.com{path}"
    if query:
        url += "?" + query
    hdrs = dict(headers or {})
    hdrs["Content-Type"] = "application/json"
    hdrs["Referer"] = hdrs.get("Referer", "https://creator.douyin.com/")
    hdrs.update(extra_headers or {})
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else "{}"
    js = f"""(async () => {{
        const r = await fetch({json.dumps(url)}, {{method:'POST',headers:{json.dumps(hdrs)},body:{json.dumps(body_json)},credentials:'include'}});
        return {{status:r.status, body:await r.text()}};
    }})()"""
    try:
        result = await page.evaluate(js)
        return result
    except Exception as e:
        return {"status": -1, "body": f"page.evaluate error: {e!r}"}


def _parse_json(resp: dict) -> dict:
    if resp.get("status", 0) <= 0:
        return {"status_code": -1, "error": f"HTTP {resp.get('status',0)}", "body": str(resp.get("body", ""))[:300]}
    try:
        return json.loads(resp.get("body", "{}"))
    except Exception:
        return {"status_code": -1, "error": "json parse failed", "body": str(resp.get("body", ""))[:300]}


# ═══════════════════════════════════════════════════════════════════
# 8 步协议流程
# ═══════════════════════════════════════════════════════════════════

async def _get_sts2(page, cookies: dict, ms_token_str: str, ua: str = "") -> dict:
    """Step 1: 获取 vedit STS2 凭证。"""
    p = dict(COMMON_PARAMS, scene="web", support_h265="1", msToken=ms_token_str,
             browser_platform=_browser_platform(ua))
    path = "/aweme/mid/video/sts2"
    path_q = path + "?" + urlencode(p)

    # X-Bogus: 完全云端(远程签名服务, 无本地回退)
    xb = None
    try:
        from .sign_client import remote_xbogus
        xb = remote_xbogus(path_q)
        if xb:
            print(f"[dy-protocol] remote xbogus=OK")
    except Exception:
        xb = None
    hdrs = {"Referer": "https://creator.douyin.com/"}
    if xb:
        hdrs["x-bogus"] = xb

    resp = await _browser_get(page, path, params=p, extra_headers=hdrs)
    data = _parse_json(resp)
    if data.get("status_code") != 0:
        return {"ok": False, "error": f"STS2 failed: status_code={data.get('status_code')}"}
    missing = [k for k in ("access_key_id", "secret_access_key", "session_token") if not data.get(k)]
    if missing:
        return {"ok": False, "error": f"STS2 missing field(s) {missing}"}
    return {"ok": True, "access_key_id": data["access_key_id"],
            "secret_access_key": data["secret_access_key"],
            "session_token": data["session_token"]}


async def _get_upload_auth(page, ms_token_str: str, ua: str = "") -> dict:
    """Step 1b: 获取 VOD 上传凭证。"""
    p = dict(COMMON_PARAMS, aid="2906", msToken=ms_token_str, browser_platform=_browser_platform(ua))
    resp = await _browser_get(page, "/web/api/media/upload/auth/v5/", params=p)
    data = _parse_json(resp)
    print(f"[dy-protocol] upload_auth raw: status_code={data.get('status_code')}, keys={sorted(data.keys())}, body={str(resp.get('body',''))[:500]}")
    if data.get("status_code") != 0:
        return {"ok": False, "error": f"upload_auth_v5: status_code={data.get('status_code')}, detail={json.dumps(data)[:300]}"}
    auth_str = data.get("auth", "")
    if not auth_str:
        return {"ok": False, "error": "upload_auth_v5: no auth field"}
    try:
        auth_data = json.loads(auth_str)
    except Exception:
        return {"ok": False, "error": "upload_auth_v5: auth parse error"}
    ak = auth_data.get("AccessKeyID", "")
    sk = auth_data.get("SecretAccessKey", "")
    token_val = auth_data.get("SessionToken", "")
    if not ak:
        return {"ok": False, "error": "upload_auth_v5: no AccessKeyID in auth"}
    return {"ok": True, "access_key_id": ak, "secret_access_key": sk, "session_token": token_val}


def _apply_upload(file_size: int, uid: str, ak: str, sk: str, token: str) -> dict:
    """Step 2: ApplyUploadInner — AWS V4 签名请求 VOD。"""
    try:
        from aws_requests_auth.aws_auth import AWSRequestsAuth
    except ModuleNotFoundError:
        return {"ok": False, "error": "missing dependency aws-requests-auth"}
    host = "vod.bytedanceapi.com"
    params_dict = {
        "Action": "ApplyUploadInner", "Version": "2020-11-19",
        "SpaceName": "aweme", "FileType": "video", "IsInner": "1",
        "FileSize": str(file_size), "app_id": "2906", "user_id": uid,
    }
    auth = AWSRequestsAuth(aws_access_key=ak, aws_secret_access_key=sk,
                           aws_host=host, aws_region="cn-north-1",
                           aws_service="vod", aws_token=token)
    try:
        resp = req.get(f"https://{host}/", params=params_dict, auth=auth, timeout=15)
        data = resp.json()
        if "Result" not in data:
            rm = data.get("ResponseMetadata", {})
            if isinstance(rm, dict) and "Error" in rm:
                err = rm["Error"]
                return {"ok": False, "error": f"VOD Error: {err.get('Code','')} - {err.get('Message','')}"}
            return {"ok": False, "error": "ApplyUpload unexpected response"}
        node = data["Result"]["InnerUploadAddress"]["UploadNodes"][0]
        store = node["StoreInfos"][0]
        return {"ok": True, "vid": node["Vid"], "store_uri": store["StoreUri"],
                "auth_jwt": store["Auth"], "upload_host": node["UploadHost"],
                "session_key": node["SessionKey"]}
    except Exception as e:
        return {"ok": False, "error": f"ApplyUpload failed: {str(e)[:200]}"}


def _upload_to_tos(video_path: str, upload_host: str, store_uri: str,
                   auth_jwt: str, uid: str) -> dict:
    """Step 3: TOS 直传视频文件。"""
    if not os.path.exists(video_path):
        return {"ok": False, "error": f"File not found: {video_path}"}
    with open(video_path, "rb") as f:
        data = f.read()
    file_size = len(data)
    crc32_val = _crc32_hex(data)
    url = f"https://{upload_host}/upload/v1/{store_uri}"
    headers = {
        "Authorization": auth_jwt,
        "x-storage-u": uid,
        "Content-CRC32": crc32_val,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(file_size),
    }
    try:
        resp = req.post(url, data=data, headers=headers, timeout=120)
        body_j = resp.json()
        if body_j.get("code") in (200, 2000):
            return {"ok": True, "file_size": file_size, "crc32": crc32_val}
        return {"ok": False, "error": f"TOS upload HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"TOS upload error: {str(e)[:200]}"}


def _commit_upload(session_key: str, uid: str, ak: str, sk: str, token: str) -> dict:
    """Step 4: CommitUploadInner — AWS V4。"""
    try:
        from aws_requests_auth.aws_auth import AWSRequestsAuth
    except ModuleNotFoundError:
        return {"ok": False, "error": "missing dependency aws-requests-auth"}
    host = "vod.bytedanceapi.com"
    params_dict = {
        "Action": "CommitUploadInner", "Version": "2020-11-19",
        "SpaceName": "aweme", "app_id": "2906", "user_id": uid,
    }
    body = {"SessionKey": session_key,
            "Functions": [{"name": "GetMeta"}, {"name": "Snapshot", "input": {"SnapshotTime": 0}}]}
    auth = AWSRequestsAuth(aws_access_key=ak, aws_secret_access_key=sk,
                           aws_host=host, aws_region="cn-north-1",
                           aws_service="vod", aws_token=token)
    resp = None
    try:
        resp = req.post(f"https://{host}/", params=params_dict, json=body, auth=auth, timeout=15)
        d = resp.json()
        return {"ok": True, "vid": d["Result"]["Results"][0]["Vid"],
                "meta": d["Result"]["Results"][0].get("VideoMeta", {})}
    except Exception as e:
        snippet = resp.text[:200] if resp else ""
        return {"ok": False, "error": f"CommitUpload failed: {str(e)[:200]} body={snippet}"}


# ═══════════════════════════════════════════════════════════════════
# 图文协议发布: ImageX 上传流程（替代 VOD ApplyUpload/Commit）
# ═══════════════════════════════════════════════════════════════════
IMAGE_SERVICE_ID = "jm8ajry58r"


def _apply_image_upload(file_size: int, uid: str, ak: str, sk: str, token: str) -> dict:
    """ApplyImageUpload — AWS V4 签名请求 ImageX 获取图片上传凭证。"""
    try:
        from aws_requests_auth.aws_auth import AWSRequestsAuth
    except ModuleNotFoundError:
        return {"ok": False, "error": "missing dependency aws-requests-auth"}
    host = "imagex.bytedanceapi.com"
    params_dict = {
        "Action": "ApplyImageUpload", "Version": "2018-08-01",
        "ServiceId": IMAGE_SERVICE_ID, "app_id": "2906", "user_id": uid,
    }
    auth = AWSRequestsAuth(aws_access_key=ak, aws_secret_access_key=sk,
                           aws_host=host, aws_region="cn-north-1",
                           aws_service="imagex", aws_token=token)
    try:
        resp = req.get(f"https://{host}/", params=params_dict, auth=auth, timeout=15)
        data = resp.json()
        if "Result" not in data:
            rm = data.get("ResponseMetadata", {})
            if isinstance(rm, dict) and "Error" in rm:
                err = rm["Error"]
                return {"ok": False, "error": f"ImageX Error: {err.get('Code','')} - {err.get('Message','')}"}
            return {"ok": False, "error": "ApplyImageUpload unexpected response"}
        result = data["Result"]
        # 尝试多种响应结构: UploadAddress(直传) / InnerUploadAddress(内网上传)
        address = (result.get("UploadAddress") or result.get("InnerUploadAddress") or {})
        # UploadAddress 有 UploadNodes 数组, 也可能直接有 StoreInfos
        nodes = address.get("UploadNodes") or [address]
        store = nodes[0].get("StoreInfos", [{}])[0]
        upload_host = nodes[0].get("UploadHost", "") or (
            address.get("UploadHosts", [""])[0] if address.get("UploadHosts") else "")
        return {"ok": True, "store_uri": store.get("StoreUri", ""),
                "auth_jwt": store.get("Auth", ""), "upload_host": upload_host,
                "session_key": address.get("SessionKey", "")}
    except Exception as e:
        return {"ok": False, "error": f"ApplyImageUpload failed: {str(e)[:200]}"}


def _upload_image_to_cdn(image_path: str, upload_host: str, store_uri: str,
                         auth_jwt: str, uid: str) -> dict:
    """PUT 图片到 ImageX CDN。"""
    if not os.path.exists(image_path):
        return {"ok": False, "error": f"File not found: {image_path}"}
    with open(image_path, "rb") as f:
        data = f.read()
    file_size = len(data)
    crc32_val = _crc32_hex(data)
    url = f"https://{upload_host}/{store_uri}"
    headers = {
        "Authorization": auth_jwt,
        "x-storage-u": uid,
        "Content-CRC32": crc32_val,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(file_size),
    }
    try:
        resp = req.put(url, data=data, headers=headers, timeout=120)
        body_j = resp.json()
        # ImageX 上传成功判断: code=200 或 error_code=0
        if body_j.get("code") in (200, 2000) or body_j.get("error", {}).get("error_code") == 0:
            return {"ok": True, "store_uri": store_uri, "file_size": file_size, "crc32": crc32_val}
        return {"ok": False, "error": f"CDN upload HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"CDN upload error: {str(e)[:200]}"}


def _gen_creation_id() -> str:
    """生成 creation_id: 随机字符串 + 时间戳。"""
    import random as _random
    import string as _string
    rand_part = "".join(_random.choices(_string.ascii_lowercase + _string.digits, k=7))
    return f"{rand_part}{int(time.time() * 1000)}"

async def _gen_cover(page, timestamp_ms: int, ua: str = "") -> dict:
    """Step 5: 生成封面 + creation_id。"""
    creation_id = f"l4k45rri{timestamp_ms}"
    p = dict(COMMON_PARAMS, creation_id=creation_id, aid="1128", support_h265="1",
             browser_platform=_browser_platform(ua))
    resp = await _browser_get(page, "/aweme/v1/cover/gen/ref", params=p)
    print(f"[dy-protocol] cover raw: status={resp.get('status')}, body={str(resp.get('body',''))[:300]}")
    data = _parse_json(resp)
    if data.get("status_code") == 0:
        return {"ok": True, "creation_id": creation_id,
                "poster": data.get("poster", ""), "cover_url": data.get("cover_url", "")}
    return {"ok": False, "error": data.get("error", "Cover gen failed")}


async def _poll_fast_detect(page, vid: str, max_wait: int = 30) -> dict:
    """Step 6: 内容检测轮询（缩短超时，失败不阻塞发布）。"""
    poll_body = {"resource_list": [{"type": 2, "video_id": vid, "duration": 0, "cover_uri": ""}],
                 "source": 1, "is_redetect": False}
    for i in range(max_wait // 5):
        resp = await _browser_post(page, "/aweme/v1/post_assistant/fast_detect/poll", body=poll_body)
        data = _parse_json(resp)
        sc = data.get("base_resp", {}).get("status_code")
        status = data.get("status")
        has_done = data.get("has_done")
        print(f"[dy-protocol] detect poll #{i+1}: status_code={sc}, status={status}, has_done={has_done}, keys={sorted(data.keys())}")
        if sc == 0:
            if status == 1 and has_done:
                return {"ok": True, "vid": vid}
            # 明确失败（如 status_code=8 未登录等），提前退出不浪费轮询
            if status in (-1, 2, 3):
                print(f"[dy-protocol] detect: definite failure (status={status}), skipping poll")
                break
        else:
            # 非 0 且非0也不是业务成功的resp, 看是不是接口不可用
            if i == 0:
                print(f"[dy-protocol] detect: unexpected base_resp status_code={sc}, continuing poll anyway")
        await asyncio.sleep(5)
    return {"ok": False, "error": "Fast detect poll timeout"}


async def _check_publish_limits(page) -> dict:
    """Step 7: 发布限额检查。"""
    p = {"device_platform": "pc", "aid": "1128", "support_h265": "1"}
    resp = await _browser_post(page, "/aweme/v1/open/publish/limit_app_groups",
                               params=p, body={})
    data = _parse_json(resp)
    if data.get("status_code") == 0:
        return {"ok": True}
    return {"ok": False, "error": "Publish limits check failed"}


async def _create_video(page, cookies: dict, vid: str, creation_id: str,
                        title: str, description: str, tags: str,
                        cover: dict, ua: str = "",
                        visibility: str = "public", allow_save: bool = True,
                        image_uris: list = None) -> dict:
    """Step 8: create_v2 发布作品（视频/图文统一入口）。
    image_uris 非空 → media_type=2 图文；否则 media_type=4 视频。
    """
    original_title = (title or "").strip()
    text, tag_list = _build_publish_text(original_title, description, tags)
    caption_parts = [part for part in ((description or "").strip(),
                     " ".join(f"#{tag}" for tag in tag_list)) if part]
    caption = " ".join(caption_parts)
    text_extra, challenges = _build_hashtag_metadata(text, caption, tag_list)

    vis_map = {"public": 0, "friends": 2, "private": 1}
    visibility_type = vis_map.get(visibility, 0)
    download = 1 if allow_save else 0

    is_image = bool(image_uris)
    media_type = 2 if is_image else 4

    common = {
        "text": text, "caption": caption,
        "creation_id": creation_id, "media_type": media_type,
        "visibility_type": visibility_type, "download": download, "timing": 0,
        "music_source": 0, "activity": "[]", "text_extra": text_extra,
        "challenges": challenges, "mentions": "[]",
        "hashtag_source": "recommend/search",
        "hot_sentence": "", "interaction_stickers": "[]", "source_info": "{}",
    }
    if not is_image:
        common["item_title"] = original_title
        common["video_id"] = vid
    else:
        common["timing"] = -1
        common["images"] = image_uris

    cover_obj = {
        "poster": cover.get("poster", ""),
    }
    if not is_image:
        cover_obj["coverUrl"] = cover.get("cover_url", "")
        cover_obj["cover_tools_extend_info"] = "{}"
        cover_obj["cover_tools_info"] = "{}"

    pub_body: dict = {
        "item": {
            "common": common,
            "cover": cover_obj,
            "anchor": {},
        }
    }
    if not is_image:
        pub_body["item"]["mix"] = {}
        pub_body["item"]["selected_member"] = {"is_selected_member_video": False}
        pub_body["item"]["chapter"] = {"chapter": "{}"}
        pub_body["item"]["sync"] = {"should_sync": False, "sync_to_toutiao": 0}
        pub_body["item"]["open_platform"] = {}
        pub_body["item"]["assistant"] = {"is_preview": 0, "is_post_assistant": 1}

    ms_result = _ms_token.get_ms_token(cookies, ms_appid="2906", ua=ua)
    ms_token_str = ms_result.get("ms_token", "") if ms_result.get("ok") else (cookies.get("msToken", "") or "")

    params = dict(COMMON_PARAMS, read_aid="2906", screen_width="1280", screen_height="800",
                  browser_language="zh-CN", browser_platform=_browser_platform(ua),
                  browser_name="Mozilla", browser_version="5.0",
                  browser_online="true", timezone_name="Asia/Shanghai",
                  aid="1128", support_h265="1", msToken=ms_token_str)

    base_path = "/web/api/media/aweme/create_v2/"
    base_url = f"https://creator.douyin.com{base_path}"

    hdrs = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://creator.douyin.com",
        "Referer": "https://creator.douyin.com/creator-micro/content/post/image?enter_from=publish_page" if is_image else
                   "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
        "bd-ticket-guard-version": "2",
        "bd-ticket-guard-web-version": "2",
        "bd-ticket-guard-web-sign-type": "1" if is_image else "0",
        "x-secsdk-csrf-token": _csrf_token(cookies),
    }
    if is_image:
        # 从 cookie 提取 ticket guard 公钥
        guard_data = cookies.get("bd_ticket_guard_client_data_v2", "")
        if guard_data:
            try:
                import base64 as _b64
                decoded = json.loads(_b64.b64decode(guard_data + "=" * (4 - len(guard_data) % 4)))
                pub_key = decoded.get("ree_public_key", "")
                if pub_key:
                    hdrs["bd-ticket-guard-ree-public-key"] = pub_key
            except Exception:
                pass

    async def _post_create(current_body: dict):
        body_json = json.dumps(current_body, ensure_ascii=False, separators=(",", ":"))
        signed_params = dict(params)
        url_without_ab = base_url + "?" + urlencode(signed_params)
        # create_v2 a_bogus: 完全云端(远程签名服务, 无本地回退)
        from .sign_client import remote_abogus
        ab = remote_abogus(urlencode(signed_params), ua)
        if not ab:
            raise RuntimeError(
                "抖音签名服务不可用: remote_abogus 未返回(请检查 SIGN_SERVICE_URL)")
        print(f"[dy-protocol] remote abogus=OK")
        signed_params["a_bogus"] = ab

        query = urlencode(signed_params)
        url = f"https://creator.douyin.com{base_path}?{query}"
        print(f"[dy-protocol] create_v2 url (full): {url}")
        print(f"[dy-protocol] create_v2 page url: {page.url}")
        all_hdrs = dict(hdrs)
        all_hdrs["Content-Type"] = "application/json"
        print(f"[dy-protocol] create_v2 request headers: {json.dumps(all_hdrs, ensure_ascii=False)}")
        js = f"""(async () => {{
            const r = await fetch({json.dumps(url)}, {{method:'POST',headers:{json.dumps(all_hdrs)},body:{json.dumps(body_json)},credentials:'include'}});
            const respHeaders = {{}};
            r.headers.forEach((v, k) => {{ respHeaders[k] = v; }});
            return {{status:r.status, statusText:r.statusText, body:await r.text(), respHeaders:respHeaders}};
        }})()"""
        resp = await page.evaluate(js)
        print(f"[dy-protocol] create_v2 response: status={resp.get('status')}, body_len={len(resp.get('body',''))}, body_preview={str(resp.get('body',''))[:300]}")
        print(f"[dy-protocol] create_v2 response headers: {json.dumps(resp.get('respHeaders', {}), ensure_ascii=False)}")
        return resp

    resp = await _post_create(pub_body)
    try:
        data = json.loads(resp.get("body", "{}"))
    except Exception:
        return {"ok": False, "error": f"create_v2: invalid JSON response (HTTP {resp.get('status')}) body={str(resp.get('body',''))[:300]}"}

    if data.get("status_code") == 0:
        return {"ok": True, "item_id": data.get("item_id", "")}
    if data.get("status_code") == 48 and "item_title" in pub_body["item"]["common"]:
        logger.warning(f"[publish] create_v2 item_title rejected, retrying... detail={json.dumps(data)[:200]}")
        pub_body["item"]["common"].pop("item_title", None)
        resp = await _post_create(pub_body)
        try:
            data = json.loads(resp.get("body", "{}"))
        except Exception:
            return {"ok": False, "error": f"create_v2 retry: invalid JSON"}
        if data.get("status_code") == 0:
            return {"ok": True, "item_id": data.get("item_id", "")}
    return {"ok": False, "error": f"create_v2: status_code={data.get('status_code')}, detail={json.dumps(data)[:300]}"}


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════

async def publish_douyin_protocol(
    mgr: BrowserManager, identity: Identity,
    account_id: int, storage_state_json: str,
    media_type: str, title: str, desc: str,
    media_paths: List[str], topics: str = "",
    visibility: str = "public", allow_save: bool = True,
    timeout_seconds: int = 360,
) -> Tuple[bool, str, str]:
    """协议模式发布抖音作品。返回 (ok, item_id_or_error, error_detail)。

    走 8 步协议流程，HTTP 请求通过浏览器 page.evaluate(fetch) 发送。
    签名（X-Bogus / a_bogus）由远程签名服务 js-sign-service 提供。
    """
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件"

    cookie_dict = {}
    try:
        state = json.loads(storage_state_json or "{}")
        for c in state.get("cookies", []):
            cookie_dict[c.get("name", "")] = c.get("value", "")
    except Exception:
        pass

    if not cookie_dict.get("sessionid"):
        return False, "", "登录态缺少 sessionid，请重新登录该抖音账号"

    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()

    page_uid = ""
    try:
        await page.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"[dy-protocol] page url after goto: {page.url}")

        cookies_list = await ctx.cookies()
        for c in cookies_list:
            v = c.get("value", "")
            if v:
                cookie_dict[c.get("name", "")] = v
        has_session = bool(cookie_dict.get("sessionid"))
        print(f"[dy-protocol] cookies count={len(cookie_dict)}, has_sessionid={has_session}, keys={sorted(cookie_dict.keys())[:20]}")

        # 尝试从浏览器 localStorage 提取 msToken（最可靠来源）
        try:
            xmst = await page.evaluate("localStorage.getItem('xmst') || ''")
            if xmst:
                cookie_dict["msToken"] = xmst
                _ms_token.set_cached_ms_token(xmst)
                print(f"[dy-protocol] msToken extracted from browser localStorage (len={len(xmst)})")
        except Exception:
            pass

        uid = cookie_dict.get("uid_tt", "") or identity.account_id or ""
        page_uid = uid

        ms_result = _ms_token.get_ms_token(cookie_dict, ms_appid="2906", ua=identity.ua)
        ms_token_str = ms_result.get("ms_token", "") if ms_result.get("ok") else (cookie_dict.get("msToken", "") or "")
        print(f"[dy-protocol] msToken via {ms_result.get('source','?')}{(ms_token_str and ' (ok)') or ''}")

        video_path = files[0]
        file_size = os.path.getsize(video_path)
        print(f"[dy-protocol] 视频: {video_path} ({file_size} bytes)")

        print("[dy-protocol] Step 1: STS2")
        sts = await _get_sts2(page, cookie_dict, ms_token_str, ua=identity.ua)
        if not sts["ok"]:
            return False, "", f"STS2: {sts['error']}"

        print("[dy-protocol] Step 1b: Upload Auth")
        vod_auth = await _get_upload_auth(page, ms_token_str, ua=identity.ua)
        if not vod_auth["ok"]:
            return False, "", f"UploadAuth: {vod_auth['error']}"
        ak, sk, vtoken = vod_auth["access_key_id"], vod_auth["secret_access_key"], vod_auth["session_token"]

        print("[dy-protocol] Step 2: ApplyUpload")
        apply_res = await asyncio.to_thread(_apply_upload, file_size, uid, ak, sk, vtoken)
        if not apply_res["ok"]:
            return False, "", f"ApplyUpload: {apply_res['error']}"
        vid = apply_res["vid"]
        print(f"[dy-protocol] vid={vid}")

        print("[dy-protocol] Step 3: TOS Upload")
        upload_res = await asyncio.to_thread(_upload_to_tos, video_path, apply_res["upload_host"],
                                             apply_res["store_uri"], apply_res["auth_jwt"], uid)
        if not upload_res["ok"]:
            return False, "", f"TOS Upload: {upload_res['error']}"

        print("[dy-protocol] Step 4: Commit")
        commit_res = await asyncio.to_thread(_commit_upload, apply_res["session_key"], uid, ak, sk, vtoken)
        if not commit_res["ok"]:
            return False, "", f"Commit: {commit_res['error']}"
        vid = commit_res["vid"]

        print("[dy-protocol] Step 5: Cover")
        cover_res = await _gen_cover(page, int(time.time() * 1000), ua=identity.ua)
        if not cover_res["ok"]:
            print(f"[dy-protocol] Cover gen failed: {cover_res.get('error','')[:100]} — using empty cover")
            cover_res = {"ok": True, "creation_id": f"l4k45rri{int(time.time()*1000)}",
                         "poster": "", "cover_url": ""}

        print("[dy-protocol] Step 6: Fast Detect (polling...max 120s)")
        poll_res = await _poll_fast_detect(page, vid)
        if not poll_res["ok"]:
            print("[dy-protocol] Fast detect timeout — proceeding anyway")

        print("[dy-protocol] Step 7: Limits")
        await _check_publish_limits(page)

        print("[dy-protocol] Step 8: Create (signing + posting)")
        create_res = await _create_video(page, cookie_dict, vid, cover_res["creation_id"],
                                          title, desc, topics, cover_res, ua=identity.ua,
                                          visibility=visibility, allow_save=allow_save)
        if not create_res.get("ok"):
            return False, "", f"Create: {create_res.get('error','')}"

        item_id = create_res["item_id"]
        print(f"[dy-protocol] SUCCESS item_id={item_id}")
        return True, item_id, ""

    except Exception as e:
        import traceback
        print(f"[dy-protocol] 异常:\n{traceback.format_exc()}")
        return False, "", f"协议发布异常: {type(e).__name__}: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def publish_douyin_image_protocol(
    mgr: BrowserManager, identity: Identity,
    account_id: int, storage_state_json: str,
    title: str, desc: str,
    image_paths: List[str], topics: str = "",
    visibility: str = "public", allow_save: bool = True,
    timeout_seconds: int = 360,
) -> Tuple[bool, str, str]:
    """协议模式发布抖音图文。返回 (ok, item_id_or_error, error_detail)。"""
    files = [str(Path(p)) for p in image_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地图片文件"
    if len(files) > 35:
        return False, "", f"图片数量 {len(files)} 超过上限 35 张"

    cookie_dict = {}
    try:
        state = json.loads(storage_state_json or "{}")
        for c in state.get("cookies", []):
            cookie_dict[c.get("name", "")] = c.get("value", "")
    except Exception:
        pass

    if not cookie_dict.get("sessionid"):
        return False, "", "登录态缺少 sessionid，请重新登录该抖音账号"

    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()

    try:
        await page.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        # 页面可能未正确跳转, 兜底直接导航到 home
        page_url = page.url
        if "creator-micro/home" not in page_url and "creator-micro" not in page_url:
            await page.goto("https://creator.douyin.com/creator-micro/home", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        print(f"[dy-image-protocol] page url after goto: {page.url}")

        cookies_list = await ctx.cookies()
        for c in cookies_list:
            v = c.get("value", "")
            if v:
                cookie_dict[c.get("name", "")] = v
        print(f"[dy-image-protocol] cookies count={len(cookie_dict)}, has_sessionid={bool(cookie_dict.get('sessionid'))}")

        try:
            xmst = await page.evaluate("localStorage.getItem('xmst') || ''")
            if xmst:
                cookie_dict["msToken"] = xmst
                _ms_token.set_cached_ms_token(xmst)
        except Exception:
            pass

        uid = cookie_dict.get("uid_tt", "") or identity.account_id or ""
        ms_result = _ms_token.get_ms_token(cookie_dict, ms_appid="2906", ua=identity.ua)
        ms_token_str = ms_result.get("ms_token", "") if ms_result.get("ok") else (cookie_dict.get("msToken", "") or "")
        print(f"[dy-image-protocol] msToken via {ms_result.get('source','?')}{(ms_token_str and ' (ok)') or ''}")

        print(f"[dy-image-protocol] 图片数量: {len(files)}")

        print("[dy-image-protocol] Step 1: Upload Auth (STS2 + upload_auth)")
        sts = await _get_sts2(page, cookie_dict, ms_token_str, ua=identity.ua)
        if not sts["ok"]:
            return False, "", f"STS2: {sts['error']}"

        print("[dy-image-protocol] Step 1b: Upload Auth V5")
        vod_auth = await _get_upload_auth(page, ms_token_str, ua=identity.ua)
        if not vod_auth["ok"]:
            return False, "", f"UploadAuth: {vod_auth['error']}"

        # 尝试两套凭证: STS2（含ImageX权限的浏览器会话） -> upload_auth（VOD上传凭证）回退
        def _try_apply_image(file_size: int, uid_str: str):
            """用不同凭证组合尝试 ApplyImageUpload。"""
            for ak, sk, token, label in [
                (sts["access_key_id"], sts["secret_access_key"], sts["session_token"], "STS2"),
                (vod_auth["access_key_id"], vod_auth["secret_access_key"], vod_auth["session_token"], "upload_auth"),
            ]:
                result = _apply_image_upload(file_size, "", ak, sk, token)
                if result["ok"]:
                    return result, label
                # AccessDenied 则尝试下一组
                if "AccessDenied" not in result.get("error", ""):
                    return result, label
            return {"ok": False, "error": "All credential sources failed for ApplyImageUpload"}, "all"

        image_uris = []
        for i, img_path in enumerate(files):
            file_size = os.path.getsize(img_path)
            print(f"[dy-image-protocol] Step 2.{i+1}: ApplyImageUpload for {img_path} ({file_size} bytes)")
            apply_res, cred_label = _try_apply_image(file_size, uid)
            if not apply_res["ok"]:
                return False, "", f"ApplyImageUpload #{i+1} ({cred_label}): {apply_res['error']}"
            store_uri = apply_res["store_uri"]
            print(f"[dy-image-protocol]   via {cred_label} store_uri={store_uri}")

            print(f"[dy-image-protocol] Step 3.{i+1}: Upload to CDN")
            upload_res = await asyncio.to_thread(_upload_image_to_cdn, img_path,
                                                 apply_res["upload_host"], store_uri,
                                                 apply_res["auth_jwt"], uid)
            if not upload_res["ok"]:
                return False, "", f"CDN upload #{i+1}: {upload_res['error']}"

            # 获取图片宽高
            width, height = 0, 0
            try:
                from PIL import Image
                import io as _io
                with open(img_path, "rb") as f:
                    img = Image.open(_io.BytesIO(f.read()))
                    width, height = img.size
            except Exception:
                pass
            image_uris.append({"uri": store_uri, "width": width, "height": height})
            print(f"[dy-image-protocol]   uploaded OK {width}x{height}")

        print("[dy-image-protocol] Step 7: Limits")
        await _check_publish_limits(page)

        print("[dy-image-protocol] Step 8: Create (image)")
        creation_id = _gen_creation_id()
        cover_data = {"poster": image_uris[0]["uri"]}
        create_res = await _create_video(page, cookie_dict, "", creation_id,
                                          title, desc, topics, cover_data,
                                          ua=identity.ua,
                                          visibility=visibility, allow_save=allow_save,
                                          image_uris=image_uris)
        if not create_res.get("ok"):
            return False, "", f"Create: {create_res.get('error','')}"

        item_id = create_res["item_id"]
        print(f"[dy-image-protocol] SUCCESS item_id={item_id}")
        return True, item_id, ""

    except Exception as e:
        import traceback
        print(f"[dy-image-protocol] 异常:\n{traceback.format_exc()}")
        return False, "", f"协议发布异常: {type(e).__name__}: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass
