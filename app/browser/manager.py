"""Playwright 浏览器管理器(多账号隔离版)。

每个账号一套**独立持久化 context**(launch_persistent_context):
  - 独立 user-data-dir(cookie/localStorage 天然隔离)
  - 独立代理 / UA / 视口 / 时区 / 指纹
常驻这些 context 并按 LRU 控制同时存活数量(省内存)。
登录/发布用同一 profile 的**有头** context(headless=False)。
对应原项目用 chromedp 的角色。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import BrowserContext, async_playwright

from ..windowing import (CHROMIUM_WINDOW_CLASSES, bring_window_to_front,
                         capture_window_snapshot)
from .identity import Identity, fingerprint_script
from .fingerprint_store import FingerprintDbClient, host_platform_key
from .cdp import (CdpLaunchError, CdpProfileConflictError, CdpProxyError,
                  CdpProxyAuthController, XhsCdpBackend)
from .proxy import ProxyConfigError, ProxyPlan, try_proxy_plan
from .xhs_interaction import XhsInteractionPolicy, XhsVisibleActionGate

_PROXY_WEBRTC_ARGS = [
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]

# 存量 legacy 账号必须保持原来的启动参数，避免已建立的浏览器画像突然漂移。
# 注: --disable-blink-features=AutomationControlled 在 Chrome 127+ 已失效并触发
# "不受支持的命令行标记"横幅 (chromium issue 40537366), 已移除; webdriver 防检测
# 由 add_init_script(fingerprint_script) 承担, 与 Chrome 版本无关。
_LEGACY_ARGS = [
    "--disable-infobars",
    # 关键:禁止 WebRTC 走非代理 UDP。否则真实 Chromium 会通过 STUN 直接暴露宿主
    # 公网/内网 IP,绕过我们在 HTTP 层设的账号代理 —— 所有号在 WebRTC 上露同一真实
    # 出口 IP,一号一代理的防关联就白做了。这个 flag 让 WebRTC 只认代理路径。
    *_PROXY_WEBRTC_ARGS,
]

# storage_state 里允许注入的 Cookie 字段(playwright add_cookies 接受的键)
_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def _parse_proxy(s: str) -> Optional[Dict[str, str]]:
    """把 http://user:pass@host:port / socks5://host:port 解析成 Playwright proxy 配置。"""
    plan = try_proxy_plan(s)
    return plan.playwright() if plan else None


def normalize_proxy(s: str) -> str:
    """把用户输入规范成带协议头的代理 URL(httpx 必须带 scheme)。
    裸 host:port -> http://host:port;保留账号密码;无法解析则原样返回。
    例:'1.2.3.4:8080' -> 'http://1.2.3.4:8080'。"""
    s = (s or "").strip()
    plan = try_proxy_plan(s)
    return plan.normalized if plan else s


def _sanitize_cookies(cookies: List[dict]) -> List[dict]:
    out = []
    for c in cookies:
        if not c.get("name"):
            continue
        ck = {k: c[k] for k in _COOKIE_KEYS if k in c}
        if ck.get("sameSite") not in ("Strict", "Lax", "None"):
            ck.pop("sameSite", None)
        out.append(ck)
    return out


def _cookie_key(cookie: dict) -> tuple[str, str, str]:
    """Cookie identity used when merging DB storage_state into a profile."""
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or "").lstrip(".").lower(),
        str(cookie.get("path") or "/"),
    )


def _bridge_cookies(states: tuple, existing: List[dict] | None = None) -> List[dict]:
    """Return usable storage_state cookies missing from the live profile.

    Chromium removes session cookies when a persistent context is closed.  A
    profile can therefore be non-empty while an auth cookie captured in
    ``storage_state`` is already absent.  This is common for Channels'
    ``_finder_auth``/``sessionid`` hand-off from the headed login context to
    the background context.

    Existing profile cookies win so an older DB snapshot never overwrites a
    cookie Chromium has refreshed since the last snapshot.
    """
    existing_keys = {_cookie_key(c) for c in (existing or [])}
    candidates: Dict[tuple[str, str, str], dict] = {}
    now = time.time()
    for raw_state in states or ():
        try:
            cookies = json.loads(raw_state or "{}").get("cookies") or []
        except Exception:
            continue
        for cookie in _sanitize_cookies(cookies):
            key = _cookie_key(cookie)
            if not key[0] or key in existing_keys:
                continue
            expires = cookie.get("expires")
            try:
                if expires is not None and float(expires) > 0 and float(expires) <= now:
                    continue
            except (TypeError, ValueError):
                pass
            candidates[key] = cookie
    return list(candidates.values())


class BrowserManager:
    def __init__(self, default_ua: str, profiles_root: str = "./data/profiles",
                 max_live: int = 6, native_ua_callback=None,
                 xhs_browser_mode: str = "auto",
                 xhs_cdp_idle_seconds: int = 900,
                 fingerprint_db_dir: str = "",
                 fingerprint_db_base_url: str = "",
                 fingerprint_db_read_key: str = ""):
        self.default_ua = default_ua
        self.profiles_root = profiles_root
        self.max_live = max(1, max_live)
        self._native_ua_callback = native_ua_callback
        self.xhs_browser_mode = (
            xhs_browser_mode if xhs_browser_mode in {"auto", "cdp", "playwright"}
            else "auto"
        )
        self.xhs_cdp_idle_seconds = max(0, int(xhs_cdp_idle_seconds))
        # fingerprint-db 数据源 (API 优先, 文件回退)
        self._fingerprint_db_dir = fingerprint_db_dir
        self._fpdb_client = FingerprintDbClient(
            fingerprint_db_base_url, fingerprint_db_read_key)
        self._pw = None
        self._contexts: Dict[Any, BrowserContext] = {}   # key -> 持久化 context
        self._cdp_sessions: Dict[Any, Any] = {}
        self._backend_by_key: Dict[Any, str] = {}
        self._fallback_reason_by_key: Dict[Any, str] = {}
        self._proxy_signature_by_key: Dict[Any, str] = {}
        self._cdp_backend = None
        self.xhs_interaction = XhsInteractionPolicy()
        self._xhs_visible_gate = XhsVisibleActionGate()
        self._xhs_page_locks: Dict[Any, asyncio.Lock] = {}
        self._last_used: Dict[Any, float] = {}
        self._locks: Dict[Any, asyncio.Lock] = {}
        self._cv_lock = asyncio.Lock()                   # 保护 context 字典的创建/驱逐
        self._chrome_major: Optional[int] = None         # 实际 Chromium 大版本(启动时探测)
        # 优先使用机器上安装的稳定版 Chrome；没有时回退到 Playwright
        # 附带的 Chrome for Testing。登录窗口因此更接近用户日常浏览器环境。
        self._browser_channel: Optional[str] = None
        self._channels: Dict[str, int] = {}   # channel → Chromium 大版本 (探测结果)
        self._shardx_sdk = None                # ShardX SDK 实例 (惰性, 抖音引擎级方案)

    async def start(self):
        self._pw = await async_playwright().start()
        self._chrome_major = await self._detect_chrome_major()
        self._cdp_backend = XhsCdpBackend(
            self._pw, self.profiles_root)

    async def _detect_chrome_major(self) -> Optional[int]:
        """探测本机可用浏览器 channel 并读取真实 Chromium 大版本。

        优先系统稳定版 Chrome/Edge，缺失时使用 Playwright bundled Chromium。
        账号 UA 池写死了 Chrome/Edg 版本,但真实内核可能是另一版本 —— 二者不一致时,
        Sec-CH-UA 请求头 / navigator.userAgentData 由真实内核发出,会和 UA 字符串对不上,
        成为自动化特征。这里读一次真实 UA,后续把账号 UA 的版本号归一到它。
        channel 按账号 UA 分配 (Edg/→msedge, Chrome/→chrome), 不同内核渲染路径不同,
        产生真实不同的 canvas/WebGL 指纹, 支持多账号防关联。
        """
        self._channels: Dict[str, int] = {}
        for channel in ("chrome", "msedge", None):
            launch_kwargs: Dict[str, Any] = {
                "headless": True,
                # Playwright 默认注入 --no-sandbox 触发 Chrome 127+ 横幅警告,
                # Windows 沙箱可用, 显式移除以恢复沙箱安全性 (不影响指纹)。
                "ignore_default_args": ["--no-sandbox"],
            }
            if channel:
                launch_kwargs["channel"] = channel
            try:
                browser = await self._pw.chromium.launch(**launch_kwargs)
            except Exception:
                continue
            try:
                pg = await browser.new_page()
                ua = await pg.evaluate("navigator.userAgent")
                m = re.search(r"Chrome/(\d+)", ua or "")
                self._channels[channel or "chromium"] = int(m.group(1)) if m else 0
            except Exception:
                pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        self._browser_channel = next(
            (c for c in ("chrome", "msedge") if c in self._channels), None)
        self._chrome_major = self._channels.get(self._browser_channel or "", 0) or None
        return self._chrome_major

    def _channel_for_ua(self, ua: str) -> Optional[str]:
        """按账号 UA 分配浏览器 channel (Edg/→msedge, Chrome/→chrome)。"""
        if "Edg/" in ua and "msedge" in self._channels:
            return "msedge"
        if "chrome" in self._channels:
            return "chrome"
        return None

    @staticmethod
    def _rendering_mode_for(identity: Identity) -> str:
        """按账号 seed 确定性分配渲染模式: gpu | swiftshader。

        同一 GPU 下 canvas/WebGL 指纹由硬件决定, 任何浏览器 channel 都相同;
        SwiftShader 软件渲染走完全不同的光栅化路径 → 指纹真实不同。
        seed 固定 → 同账号每次一致。
        """
        import random as _rnd
        rnd = _rnd.Random(identity.fp_seed or "0")
        return "swiftshader" if rnd.random() < 0.5 else "gpu"

    def _normalize_ua(self, ua: str) -> str:
        """把账号 UA 的 Chrome/Edg 大版本对齐到真实内核版本(未探测到则原样返回)。

        channel 按 UA 匹配 (Edg/→msedge, Chrome/→chrome), 用对应内核版本归一化,
        保证 UA 字符串与真实内核 / Sec-CH-UA 一致。
        """
        if not self._channels or not ua:
            return ua
        ch = self._channel_for_ua(ua)
        v = self._channels.get(ch or "", 0)
        if not v:
            return ua
        ua = re.sub(r"Chrome/\d+", f"Chrome/{v}", ua)
        ua = re.sub(r"Edg/\d+", f"Edg/{v}", ua)
        return ua

    def environment_snapshot(self, identity: Identity, *, headless: bool) -> Dict[str, Any]:
        """返回不含代理凭据的浏览器环境诊断信息。"""
        backend = self._backend_by_key.get(identity.key)
        if backend is None:
            backend = (
                "cdp" if self._uses_xhs_cdp(identity)
                else "playwright"
            )
        is_cdp = backend == "cdp"
        fallback_reason = self._fallback_reason_by_key.get(identity.key, "")
        # Diagnostics are user-facing, never a transport for debugger URLs or
        # proxy credentials.
        fallback_reason = re.sub(
            r"\b(?:ws|wss)://\S+", "[CDP endpoint]", fallback_reason,
            flags=re.IGNORECASE)
        fallback_reason = re.sub(
            r"\b127\.0\.0\.1:\d+(?:/\S*)?", "[loopback endpoint]",
            fallback_reason)
        fallback_reason = re.sub(
            r"(https?|socks5)://[^/@\s]+@", r"\1://***@",
            fallback_reason, flags=re.IGNORECASE)
        fallback = bool(fallback_reason)
        backend_label = (
            "系统 Chrome · CDP" if is_cdp
            else "Playwright Chromium · 回退" if fallback
            else "Playwright Chromium"
        )
        return {
            "browser": "chrome" if is_cdp else (self._browser_channel or "chromium"),
            "chrome_major": self._chrome_major,
            "headless": False if is_cdp else bool(headless),
            "identity_mode": identity.identity_mode,
            "profile_dir": identity.profile_dir,
            "has_proxy": bool(str(identity.proxy or "").strip()),
            "backend": backend,
            "backend_label": backend_label,
            "fallback": fallback,
            "fallback_reason": fallback_reason,
        }

    def _sec_ch_ua_headers(self, ua: str) -> Optional[Dict[str, str]]:
        """按归一后的 UA 生成一致的 Client Hints 头,覆盖真实内核默认发出的值。"""
        ch = self._channel_for_ua(ua)
        v = self._channels.get(ch or "", 0)
        if not v:
            return None
        if "Edg/" in ua:
            brands = (f'"Chromium";v="{v}", "Microsoft Edge";v="{v}", '
                      f'"Not?A_Brand";v="99"')
        else:
            brands = (f'"Chromium";v="{v}", "Google Chrome";v="{v}", '
                      f'"Not?A_Brand";v="99"')
        platform = ('"macOS"' if "Mac OS" in ua
                    else '"Linux"' if "Linux" in ua and "Android" not in ua
                    else '"Windows"')
        return {"sec-ch-ua": brands, "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": platform}

    async def stop(self):
        async with self._cv_lock:
            for key in list(self._contexts):
                await self._close_key_unlocked(key)
        if self._pw:
            await self._pw.stop()
        self._pw = None

    # ── 画像 ──
    def identity_for(self, acc) -> Identity:
        return Identity.from_account(acc, self.profiles_root, self.default_ua)

    def anon_identity(self) -> Identity:
        return Identity(account_id=None,
                        profile_dir=str(Path(self.profiles_root) / "_anon"),
                        ua=self.default_ua)

    def _uses_xhs_cdp(self, identity: Identity) -> bool:
        return (
            identity.platform == "xhs"
            and self.xhs_browser_mode in {"auto", "cdp"}
        )

    @staticmethod
    def _xhs_proxy_plan(identity: Identity) -> ProxyPlan | None:
        try:
            return ProxyPlan.parse(identity.proxy)
        except ProxyConfigError as exc:
            raise CdpProxyError(str(exc)) from None

    def lock_for(self, key) -> asyncio.Lock:
        """每账号串行锁:同一账号同一时刻只允许一个浏览器动作。"""
        return self._locks.setdefault(key, asyncio.Lock())

    # ── 持久化 context ──
    @staticmethod
    def _fingerprint_seed_from(fp_seed: str) -> int:
        """fp_seed(hex) → 确定性整数种子 (同账号每次一致)。"""
        try:
            return int(fp_seed, 16) % 2 ** 32
        except (ValueError, TypeError):
            return 10000 + (abs(hash(fp_seed or "")) % 90000)

    @staticmethod
    def _patch_pathlib_utf8() -> None:
        """shardx SDK 在 Windows 用 Path.read_text() 默认 GBK 读 UTF-8 JSON,
        monkey-patch 默认 encoding 为 UTF-8 (全局安全)。"""
        import pathlib as _pl
        if getattr(_pl.Path.read_text, "_ch_patched", False):
            return
        _orig = _pl.Path.read_text

        def _read_text(self, encoding=None, errors=None):
            if encoding is None:
                encoding = "utf-8"
            return _orig(self, encoding, errors)

        _pl.Path.read_text = _read_text
        _pl.Path.read_text._ch_patched = True  # type: ignore[attr-defined]

    @staticmethod
    def _proxy_to_url(proxy: Optional[Dict[str, str]]) -> Optional[str]:
        """Playwright proxy dict → URL 字符串 (ShardX/BrowserSession 参数格式)。"""
        if not proxy:
            return None
        server = proxy.get("server", "")
        if proxy.get("username"):
            prefix = server.split("://", 1)
            scheme = (prefix[0] + "://") if len(prefix) > 1 else ""
            host = prefix[1] if len(prefix) > 1 else server
            return f"{scheme}{proxy['username']}:{proxy.get('password', '')}@{host}"
        return server

    async def _pick_custom_profile(self, identity: Identity,
                                   platform: str = "") -> Any:
        """按 fp_seed 确定性选 fingerprint-db profile (仅走 HTTP API)。

        数据主权: 独立项目 fingerprint-db (434 套跨平台合成 profile, 真机基线+
        公开硬件规格+一致性规则), ShardX 引擎格式兼容。fp_seed 固定 →
        同账号每次同 profile/同指纹。

        平台过滤: platform 非空则按指定平台 (mac/win/linux) 选; 空则按宿主 OS。
        独立 profile 可指定 os 覆盖; 账号路径默认宿主 OS。

        数据源: fingerprint-db HTTP API (fingerprint_db_base_url 非空)。
        本地不再存指纹库, API 未配置或不可达时返回 None (回退 ShardX 自带库)。
        """
        from shardx import Profile

        if self._fpdb_client.enabled:
            key = platform or host_platform_key()
            data = await self._fpdb_client.pick(identity.fp_seed, key)
            if data is not None:
                return Profile(dict(data))
        return None

    async def _load_named_profile(self, name: str) -> Any:
        """按 name 直接加载 fingerprint-db profile (仅走 HTTP API)。"""
        from shardx import Profile

        if self._fpdb_client.enabled:
            try:
                data = await self._fpdb_client.load_profile(name)
                return Profile(dict(data))
            except Exception:
                pass
        return None

    @staticmethod
    def _apply_profile_overrides(prof: Any, ua_override: str,
                                 cpu_cores: int, memory_gb: int,
                                 timezone: str, language: str,
                                 overrides: dict | None) -> Any:
        """把标量覆盖 + overrides JSON 应用到 profile config (返回新 Profile)。

        对标 ShardX Launcher 的可覆盖字段:
          - navigator.user_agent / hardware_concurrency / device_memory / language
          - timezone ("auto" 哨兵由 SDK resolve_auto_fields 处理)
          - noise (6 向量) → set_noise
          - geolocation / media_devices / network.blocked_ports / navigator.do_not_track
        """
        ov = dict(overrides or {})
        nav = {}
        if ua_override:
            nav["user_agent"] = ua_override
        if cpu_cores:
            nav["hardware_concurrency"] = int(cpu_cores)
        if memory_gb:
            nav["device_memory"] = int(memory_gb)
        if language:
            nav["language"] = language
        # overrides.navigator 合并进 navigator (如 do_not_track)
        if isinstance(ov.get("navigator"), dict):
            nav.update(ov["navigator"])

        kw: dict = {}
        if nav:
            kw["navigator"] = nav
        if timezone:
            kw["timezone"] = timezone
        for k in ("geolocation", "media_devices", "network"):
            if isinstance(ov.get(k), dict):
                kw[k] = ov[k]
        if kw:
            prof = prof.with_override(**kw)

        # noise: 6 向量三态映射 (real=关, auto/noise=开)。默认全开 Auto noise。
        noise = ov.get("noise")
        if noise is None:
            noise = {"canvas": "auto", "webgl": "auto", "audio": "auto",
                     "client_rects": "auto", "sensors": "auto", "fonts": "noise"}
        vectors = [v for v in ("canvas", "webgl", "audio",
                               "client_rects", "sensors", "fonts")
                   if (noise.get(v) in ("auto", "noise", True))]
        prof.set_noise(*vectors)
        return prof

    def _align_engine_version(self, prof: Any) -> Any:
        """把 profile 的 UA/UA-CH 版本号对齐到 ShardX 引擎真实版本(149)。

        fingerprint-db 合成 profile 的 UA 版本号随机散布(Chrome 80~140 共 57 种),
        与引擎二进制(Chromium 149)不一致。SDK 的 Browser.launch 内部会调用
        apply_engine_version 归一,但那只改内存里的 config,固化到磁盘的副本仍是
        脏版本。这里在固化前显式对齐,保证固化副本 + 运行时 UA 都统一为引擎版本,
        协议直发的签名 UA 与实际请求头 UA 因此一致。
        """
        try:
            from shardx.runtime import apply_engine_version
            runtime = self._shardx_sdk.runtime
            apply_engine_version(
                prof.config,
                runtime.chromium_version,
                runtime.grease_brand,
                runtime.grease_version,
            )
        except Exception:
            pass
        return prof

    async def _launch_shardx(self, identity: Identity, headless: bool,
                             fingerprint_name: str = "",
                             os: str = "", ua_override: str = "",
                             cpu_cores: int = 0, memory_gb: int = 0,
                             timezone: str = "", language: str = "",
                             webrtc_mode: str = "auto",
                             overrides: dict | None = None) -> BrowserContext:
        """ShardX 引擎级启动 (Chromium 149, 170 真机设备库)。

        真机 GPU/硬件/UA 模板内部一致 → BrowserScan WebGL/Audio/隐身 全过 (实测)。
        自建指纹库 (fingerprint-db) 首次由 fp_seed 确定性选定后固化为按账号唯一的
        ShardX 持久化 profile (.fpdb_id 记 id), 后续 open_profile 复用 → 同账号每次
        同指纹, 且对库演进 (增删/重生成) 免疫; 失败回退 ShardX 自带库 (.shardx_id)。

        fingerprint_name 非空时, 跳过 seed 确定性选择, 直接加载指定 fingerprint-db
        profile (独立 profile 用)。

        覆盖参数 (独立 profile 用, 账号路径默认空=不覆盖):
          os / ua_override / cpu_cores / memory_gb / timezone / language
          / webrtc_mode / overrides(noise/geolocation/media_devices/network/navigator)
        """
        self._patch_pathlib_utf8()
        from shardx import ShardX, Profile
        if self._shardx_sdk is None:
            self._shardx_sdk = ShardX()
        sdk = self._shardx_sdk
        pdir = Path(identity.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        # 自建指纹库优先 (fingerprint-db): 用 ShardX 持久化 profile 固化"账号→指纹"映射。
        # 首次由 fp_seed 确定性选一个 fingerprint-db profile, 保存为按账号唯一的 ShardX
        # 持久化 profile 并记下其 id; 后续 open_profile 直接复用冻结副本。这样映射对库演进
        # (增删/重生成 profile) 免疫 — 修复 fp_seed % len(files) 漂移, 保证同账号同指纹。
        fp_id_file = pdir / ".fpdb_id"
        prof = None
        # 0) 账号绑定独立 profile 时: 优先 open_profile(shardx_id) 复用该指纹, 跳过 fp_seed 随机
        if identity.shardx_id:
            try:
                prof = sdk.open_profile(identity.shardx_id)
                print(f"[browser] shardx profile: 绑定复用 {identity.shardx_id}")
            except Exception:
                prof = None
        # 1) 自建指纹库固化 (.fpdb_id) — 账号首次 fp_seed 选型后的冻结副本
        if prof is None and fp_id_file.exists():
            fid = fp_id_file.read_text(encoding="utf-8").strip()
            if fid:
                try:
                    prof = sdk.open_profile(fid)
                except Exception:
                    prof = None
        # 2) 确定性选择 (fingerprint-db API 或文件)
        if prof is None:
            picked = None
            if fingerprint_name:
                picked = await self._load_named_profile(fingerprint_name)
            else:
                picked = await self._pick_custom_profile(identity, platform=os)
            if picked is not None:
                fid = "fpdb-" + uuid.uuid4().hex
                prof = Profile(dict(picked.config), id=fid)
                # 固化前对齐 UA 版本号到引擎 149,保证冻结副本干净
                self._align_engine_version(prof)
                try:
                    sdk.save_profile(prof)
                except Exception:
                    pass
                try:
                    fp_id_file.write_text(fid, encoding="utf-8")
                except Exception:
                    pass
                print(f"[browser] shardx profile: 自建库 {picked.config.get('name')} (已固化 {fid})")
            else:
                prof = None
        else:
            print(f"[browser] shardx profile: 自建库持久化 {prof.id}")
        if prof is None:
            # 回退 ShardX 自带库 (原有 .shardx_id 逻辑)
            sid_file = pdir / ".shardx_id"
            prof = None
            if sid_file.exists():
                try:
                    prof = sdk.open_profile(sid_file.read_text(encoding="utf-8").strip())
                except Exception:
                    prof = None
            if prof is None:
                templates = sdk.list_profiles(platform="Windows")
                if not templates:
                    templates = sdk.list_profiles()
                idx = self._fingerprint_seed_from(identity.fp_seed) % len(templates)
                prof = sdk.create_profile(templates[idx])
                prof.set_noise("canvas")          # canvas 确定性噪声; audio/webgl 保持真实
                self._align_engine_version(prof)
                sdk.save_profile(prof)
                try:
                    sid_file.write_text(prof.id, encoding="utf-8")
                except Exception:
                    pass
        # 3) 应用覆盖 + noise (独立 profile; 账号路径参数为空则跳过)
        if prof is not None:
            # 对齐 UA 版本到引擎(覆盖 open_profile 读出的历史脏副本)
            self._align_engine_version(prof)
            prof = self._apply_profile_overrides(
                prof, ua_override, cpu_cores, memory_gb, timezone, language,
                overrides)
        proxy_url = self._proxy_to_url(_parse_proxy(identity.proxy))
        bsess = sdk.launch(prof, cdp=True, proxy=proxy_url, headless=headless,
                           webrtc=webrtc_mode)
        try:
            browser = await self._pw.chromium.connect_over_cdp(bsess.cdp_url)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        except Exception:
            with suppress(Exception):
                bsess.stop()
            raise
        ctx._shardx_bsess = bsess  # type: ignore[attr-defined]  # 关闭时杀进程
        ctx._shardx_id = getattr(prof, "id", "")  # type: ignore[attr-defined]  # 固化后回填 shardx_id
        # probe 实际 UA 回写 (真机模板生成, 同账号每次一致)
        probe_page = None
        try:
            pages = list(ctx.pages)
            probe_page = pages[0] if pages else await ctx.new_page()
            actual_ua = await probe_page.evaluate("navigator.userAgent")
            if actual_ua:
                identity.ua = str(actual_ua)
                if identity.account_id is not None and self._native_ua_callback:
                    self._native_ua_callback(identity.account_id, identity.ua)
        except Exception:
            pass
        finally:
            if probe_page is not None and probe_page not in list(ctx.pages):
                try:
                    await probe_page.close()
                except Exception:
                    pass
        return ctx

    async def _launch_cloak(self, identity: Identity, headless: bool) -> BrowserContext:
        """CloakBrowser 引擎级启动 (v146 免费, 无并发限制)。

        C++ 源码 58 补丁: canvas(toDataURL层seed噪声)/GPU/audio/字体/屏幕/硬件 按
        --fingerprint=seed 差异化且无痕 (getImageData 真实, 函数 native)。
        fp_seed 确定性映射 → 同账号每次同指纹 (返回访问者一致)。
        """
        from cloakbrowser import launch_persistent_context_async
        pdir = Path(identity.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        seed = self._fingerprint_seed_from(identity.fp_seed)
        proxy = _parse_proxy(identity.proxy)
        kwargs: Dict[str, Any] = dict(
            headless=headless,
            args=[
                f"--fingerprint={seed}",
                # 存储配额设为 30GB 呈现正常 profile。实测 BrowserScan:
                # 5000MB 仍被判定隐身(-10%), 30000MB 通过 — 正常 Chrome 配额
                # 通常数 GB~数十 GB, 阈值高于 README 旧示例的 5000。
                "--fingerprint-storage-quota=30000",
            ],
            viewport={"width": identity.viewport_w, "height": identity.viewport_h},
            locale=identity.locale or "zh-CN",
            timezone_id=identity.timezone_id or "Asia/Shanghai",
        )
        if proxy:
            kwargs["proxy"] = proxy
        ctx = await launch_persistent_context_async(str(pdir), **kwargs)
        # probe 实际 UA 回写 (CloakBrowser 引擎层按 seed 生成, 同账号每次一致)
        probe_page = None
        try:
            pages = list(ctx.pages)
            probe_page = pages[0] if pages else await ctx.new_page()
            actual_ua = await probe_page.evaluate("navigator.userAgent")
            if actual_ua:
                identity.ua = str(actual_ua)
                if identity.account_id is not None and self._native_ua_callback:
                    self._native_ua_callback(identity.account_id, identity.ua)
        except Exception:
            pass
        finally:
            if probe_page is not None and probe_page not in list(ctx.pages):
                try:
                    await probe_page.close()
                except Exception:
                    pass
        return ctx

    async def _launch_persistent(self, identity: Identity, headless: bool = True
                                 ) -> BrowserContext:
        # ShardX 引擎级方案 (Chromium 149, 170 真机设备库; BrowserScan WebGL/Audio/
        # 隐身全过, 实测)。抖音账号优先, 失败回退 CloakBrowser → 系统 Chrome。
        if identity.platform == "douyin":
            try:
                return await self._launch_shardx(
                    identity, headless,
                    fingerprint_name=identity.fingerprint_name,
                    os=identity.os, ua_override=identity.ua_override,
                    cpu_cores=identity.cpu_cores, memory_gb=identity.memory_gb,
                    timezone=identity.tz_override, language=identity.language_override,
                    webrtc_mode=identity.webrtc_mode, overrides=identity.overrides)
            except Exception as e:
                print(f"[browser] shardx launch failed -> cloak fallback: {e!r}")
            try:
                return await self._launch_cloak(identity, headless)
            except Exception as e:
                print(f"[browser] cloakbrowser launch failed -> fallback: {e!r}")
        pdir = Path(identity.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        was_empty = not any(pdir.iterdir())
        ua = self._normalize_ua(identity.ua or self.default_ua)
        kwargs: Dict[str, Any] = dict(
            user_data_dir=str(pdir), headless=headless,
            # Playwright 默认注入 --no-sandbox 触发 Chrome 127+ 横幅警告,
            # Windows 沙箱可用, 显式移除以恢复沙箱安全性 (不影响指纹)。
            ignore_default_args=["--no-sandbox"],
        )
        if self._channels:
            # channel 按账号 UA 分配: Edg/→msedge, Chrome/→chrome (真实渲染差异, 防关联)
            ch = self._channel_for_ua(ua)
            if ch:
                kwargs["channel"] = ch
        elif self._browser_channel:
            kwargs["channel"] = self._browser_channel
        legacy = identity.identity_mode != "native"
        if legacy:
            kwargs.update(
                args=list(_LEGACY_ARGS),
                viewport={"width": identity.viewport_w, "height": identity.viewport_h},
                locale=identity.locale or "zh-CN",
                timezone_id=identity.timezone_id or "Asia/Shanghai",
                user_agent=ua,
                # 存量账号保持既有画像，避免已建立的 profile 突然漂移。
                geolocation=identity.geolocation,
                permissions=["geolocation"],
            )
        proxy = _parse_proxy(identity.proxy)
        if not legacy:
            # native 账号使用 Chrome/操作系统自己的视口、语言、时区与硬件画像。
            # webdriver 防检测由 add_init_script(fingerprint_script) 承担 (Chrome 127+
            # 命令行参数已失效); WebRTC 约束仅在显式配置代理时加入,避免 UDP 绕过代理出口。
            kwargs["no_viewport"] = True
            native_args = []
            # 渲染模式差异化: 同一 GPU 下 canvas/WebGL 指纹天然相同(硬件决定),
            # 按账号 seed 分配 GPU / SwiftShader 软件渲染 → 真实不同的光栅化路径,
            # canvas/WebGL 指纹真实不同且检测站视为正常(无GPU机器/远程桌面即软件渲染)。
            if self._rendering_mode_for(identity) == "swiftshader":
                native_args += ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
            if proxy:
                native_args.extend(_PROXY_WEBRTC_ARGS)
            kwargs["args"] = native_args
        if proxy:
            kwargs["proxy"] = proxy
        ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        if not legacy:
            # Persist the UA exposed by this exact native context. It is
            # diagnostic state only; native launches still omit any override.
            probe_page = None
            created_probe = False
            try:
                pages = list(ctx.pages)
                if pages:
                    probe_page = pages[0]
                else:
                    probe_page = await ctx.new_page()
                    created_probe = True
                actual_ua = await probe_page.evaluate("navigator.userAgent")
                if actual_ua:
                    identity.ua = str(actual_ua)
                    if identity.account_id is not None and self._native_ua_callback:
                        self._native_ua_callback(identity.account_id, identity.ua)
            except Exception:
                pass
            finally:
                if created_probe and probe_page is not None:
                    try:
                        await probe_page.close()
                    except Exception:
                        pass
        # Client Hints 与归一后的 UA 保持一致(否则内核按真实版本发 Sec-CH-UA,和 UA 打架)
        sec = self._sec_ch_ua_headers(ua) if legacy else None
        if sec:
            try:
                await ctx.set_extra_http_headers(sec)
            except Exception:
                pass
        if legacy and identity.fp_seed:
            try:
                await ctx.add_init_script(fingerprint_script(identity.fp_seed, ua))
            except Exception:
                pass
        # 登录态桥接:
        # 1) 全新 profile 注入 DB storage_state，兼容旧账号迁移；
        # 2) 非空 profile 也补回“磁盘中缺失”的 Cookie。Chromium 关闭 context
        #    时会丢弃 session cookie，视频号刚扫码成功后从有头切到无头 context
        #    正好会经过这条路径。只补缺失项，不覆盖 profile 中更新过的 Cookie。
        await self._bridge_identity_cookies(
            ctx, identity, assume_empty=was_empty)
        return ctx

    @staticmethod
    async def _bridge_identity_cookies(
            ctx: BrowserContext, identity: Identity,
            *, assume_empty: bool = False) -> None:
        if not identity.bridge_states:
            return
        try:
            existing = [] if assume_empty else await ctx.cookies()
            cookies = _bridge_cookies(identity.bridge_states, existing)
            if cookies:
                await ctx.add_cookies(cookies)
        except Exception:
            pass

    async def _evict_if_needed(self):
        """常驻 context 超过上限时,关掉最久未用且当前未被锁占用的那个。"""
        while len(self._contexts) >= self.max_live:
            cands = [k for k in self._contexts if not self._key_locked(k)]
            if not cands:
                break
            victim = min(cands, key=lambda k: self._last_used.get(k, 0))
            await self._close_key_unlocked(victim)

    def _key_locked(self, key: Any) -> bool:
        candidates = (key, f"acc:{key}") if isinstance(key, int) else (key,)
        if any(
            candidate in self._locks and self._locks[candidate].locked()
            for candidate in candidates
        ):
            return True
        page_lock = self._xhs_page_locks.get(key)
        if page_lock is not None and page_lock.locked():
            return True
        return self._xhs_visible_gate.active_account == key

    @staticmethod
    def _cdp_session_healthy(session: Any) -> bool:
        checker = getattr(getattr(session, "browser", None), "is_connected", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    async def _close_key_unlocked(self, key: Any) -> None:
        ctx = self._contexts.pop(key, None)
        session = self._cdp_sessions.pop(key, None)
        self._last_used.pop(key, None)
        self._backend_by_key.pop(key, None)
        self._fallback_reason_by_key.pop(key, None)
        self._proxy_signature_by_key.pop(key, None)
        if session is not None and self._cdp_backend is not None:
            with suppress(Exception):
                await self._cdp_backend.close(session)
            return
        if ctx is not None:
            with suppress(Exception):
                await ctx.close()
            bsess = getattr(ctx, "_shardx_bsess", None)
            if bsess is not None:
                self._kill_shardx_engine(bsess)

    async def context_for(self, identity: Identity) -> BrowserContext:
        """取(或惰性创建)账号专属常驻 context。"""
        key = identity.key
        async with self._cv_lock:
            plan = (
                self._xhs_proxy_plan(identity)
                if identity.platform == "xhs" else None
            )
            signature = plan.signature if plan else "direct"
            ctx = self._contexts.get(key)
            session = self._cdp_sessions.get(key)
            if ctx is not None and (
                    self._proxy_signature_by_key.get(key) != signature):
                await self._close_key_unlocked(key)
                ctx = None
                session = None
            elif session is not None and not self._cdp_session_healthy(session):
                await self._close_key_unlocked(key)
                ctx = None
                session = None
            if ctx is None:
                await self._evict_if_needed()
                if self._uses_xhs_cdp(identity):
                    if self._cdp_backend is None:
                        if self._pw is None:
                            raise CdpLaunchError("浏览器管理器尚未启动")
                        self._cdp_backend = XhsCdpBackend(
                            self._pw, self.profiles_root)
                    try:
                        session = await self._cdp_backend.open(identity, plan)
                    except CdpProfileConflictError:
                        raise
                    except CdpLaunchError as exc:
                        if self.xhs_browser_mode == "cdp":
                            raise
                        self._fallback_reason_by_key[key] = str(exc)[:200]
                        ctx = await self._launch_persistent(
                            identity, headless=False)
                        self._backend_by_key[key] = "playwright"
                    else:
                        ctx = session.context
                        if plan is not None and plan.scheme in {"http", "https"} \
                                and plan.authenticated:
                            session.auth_controller = CdpProxyAuthController(
                                ctx, plan)
                        await self._bridge_identity_cookies(ctx, identity)
                        self._cdp_sessions[key] = session
                        self._backend_by_key[key] = "cdp"
                else:
                    ctx = await self._launch_persistent(
                        identity, headless=(identity.platform != "xhs"))
                    self._backend_by_key[key] = "playwright"
                self._contexts[key] = ctx
                self._proxy_signature_by_key[key] = signature
            self._last_used[key] = time.time()
            if key in self._cdp_sessions:
                self._cdp_sessions[key].last_used = self._last_used[key]
            return ctx

    async def new_page(self, identity: Identity, block_media: bool = False):
        """从账号常驻 context 开一个新 page(可屏蔽图片/视频/字体)。用完请 page.close()。"""
        ctx = await self.context_for(identity)
        page = await ctx.new_page()
        session = self._cdp_sessions.get(identity.key)
        controller = getattr(session, "auth_controller", None)
        if controller is not None:
            await controller.install(page)
        if block_media and identity.platform != "xhs":
            async def _route(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", _route)
        return page

    @asynccontextmanager
    async def visible_action(self, identity: Identity):
        """Serialize one account's page work and all visible XHS actions."""
        if self._xhs_visible_gate.owned_by_current_task:
            async with self._xhs_visible_gate.acquire(identity.key):
                yield
            return
        page_lock = self._xhs_page_locks.setdefault(
            identity.key, asyncio.Lock())
        async with page_lock:
            async with self._xhs_visible_gate.acquire(identity.key):
                yield

    @asynccontextmanager
    async def visible_page(self, identity: Identity, *, url: str = ""):
        """Lease one foreground XHS task page without closing shared Chrome."""
        async with self.visible_action(identity):
            snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
            page = None
            try:
                page = await self.new_page(identity, block_media=False)
                if url:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=30_000)
                with suppress(Exception):
                    await page.bring_to_front()
                title = ""
                with suppress(Exception):
                    title = await page.title()
                await asyncio.to_thread(
                    bring_window_to_front, snapshot,
                    CHROMIUM_WINDOW_CLASSES, title or "小红书", 1.5)
                yield page
            finally:
                if page is not None:
                    with suppress(Exception):
                        await page.close()

    async def close_context(self, key):
        async with self._cv_lock:
            await self._close_key_unlocked(key)

    async def collect_idle_cdp(self, now: float | None = None) -> int:
        if self.xhs_cdp_idle_seconds <= 0:
            return 0
        sampled = time.time() if now is None else float(now)
        async with self._cv_lock:
            victims = [
                key for key in self._cdp_sessions
                if not self._key_locked(key)
                and sampled - self._last_used.get(key, sampled)
                >= self.xhs_cdp_idle_seconds
            ]
            for key in victims:
                await self._close_key_unlocked(key)
            return len(victims)

    async def open_headed(self, identity: Identity) -> BrowserContext:
        """Return the shared headed XHS context or a temporary legacy one."""
        if identity.platform == "xhs":
            snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
            ctx = await self.context_for(identity)
            await asyncio.to_thread(bring_window_to_front, snapshot,
                                    CHROMIUM_WINDOW_CLASSES, "", 1.5)
            return ctx
        snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
        await self.close_context(identity.key)
        ctx = await self._launch_persistent(identity, headless=False)
        await asyncio.to_thread(bring_window_to_front, snapshot,
                                CHROMIUM_WINDOW_CLASSES, "", 1.5)
        return ctx

    # ── 独立 profile (ShardX Launcher 式, 与登录账号解耦) ──
    async def launch_profile(self, *, name: str, fingerprint_name: str,
                             fp_seed: str, proxy: str, profile_dir: str,
                             headless: bool = False, os: str = "",
                             ua_override: str = "", cpu_cores: int = 0,
                             memory_gb: int = 0, timezone: str = "",
                             language: str = "", webrtc_mode: str = "auto",
                             overrides: dict | None = None) -> BrowserContext:
        """启动一个独立 profile 并返回其 context (ctx._shardx_bsess.cdp_url 可用)。

        不绑定账号登录态, 纯指纹+代理+持久化目录。指纹来源:
          fingerprint_name 非空 → 固定加载该 fingerprint-db profile;
          空 → 按 fp_seed 确定性选择 (同 profile_dir 每次同指纹)。

        覆盖参数对标 ShardX Launcher (os/ua/cpu/memory/timezone/language/
        webrtc/overrides), 详见 _launch_shardx。
        """
        pdir = Path(profile_dir) if profile_dir else (
            Path(self.profiles_root) / "profile_" + uuid.uuid4().hex)
        pdir.mkdir(parents=True, exist_ok=True)
        identity = Identity(
            account_id=None, profile_dir=str(pdir),
            identity_mode="native", proxy=proxy or "",
            fp_seed=fp_seed or uuid.uuid4().hex)
        ctx = await self._launch_shardx(
            identity, headless=headless, fingerprint_name=fingerprint_name,
            os=os, ua_override=ua_override, cpu_cores=cpu_cores,
            memory_gb=memory_gb, timezone=timezone, language=language,
            webrtc_mode=webrtc_mode, overrides=overrides)
        return ctx

    @staticmethod
    def _kill_shardx_engine(bsess: Any) -> None:
        """彻底杀掉 shardx 引擎进程树(含渲染/GPU 子进程),释放 profile 目录锁。

        SDK 的 BrowserSession.stop() 只 terminate 主进程;Windows 上 Chromium
        子进程残留会继续占用 user_data_dir 锁,导致二次 launch 读不到 CDP 端点。
        这里用 taskkill /T /F 杀整棵进程树(项目 cdp.py 同款做法)。
        """
        pid = getattr(bsess, "pid", None)
        if pid is None:
            try:
                bsess.stop()
            except Exception:
                pass
            return
        if os.name == "nt":
            with suppress(Exception):
                subprocess.run(
                    ["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        # 兜底:taskkill 失败/非 Windows 时退回 SDK 的 stop()
        try:
            bsess.stop()
        except Exception:
            pass

    async def stop_profile(self, ctx: BrowserContext) -> None:
        """关闭独立 profile 的浏览器进程(杀引擎进程树)。"""
        if ctx is None:
            return
        with suppress(Exception):
            await ctx.close()
        bsess = getattr(ctx, "_shardx_bsess", None)
        if bsess is not None:
            self._kill_shardx_engine(bsess)

    async def close_login_ctx(self, ctx: BrowserContext) -> None:
        """登录流程关闭点:关 context + 杀 shardx 引擎进程树,释放 profile 目录锁。

        登录成功后抓资料/后续操作会再次启动同一 profile 目录的浏览器,
        必须彻底释放目录锁,否则二次 launch 读不到 CDP 端点。
        """
        if ctx is None:
            return
        with suppress(Exception):
            await ctx.close()
        bsess = getattr(ctx, "_shardx_bsess", None)
        if bsess is not None:
            self._kill_shardx_engine(bsess)


# 各平台 Cookie 顶域(子域如 creator./edith. 都吃顶域 cookie,一个就够)
_COOKIE_DOMAIN = {
    "douyin": ".douyin.com",
    "xhs": ".xiaohongshu.com",
    "kuaishou": ".kuaishou.com",
    "shipinhao": ".weixin.qq.com",   # 视频号:finder 登录态(_finder_auth/sessionid)挂在 .weixin.qq.com
}


def cookie_string_to_state(cookie_str: str, platform: str = "douyin") -> str:
    """把粘贴的 Cookie 串转成 Playwright storage_state JSON(兜底登录用)。"""
    domain = _COOKIE_DOMAIN.get(platform, ".douyin.com")
    cookies: List[Dict[str, Any]] = []
    for part in cookie_str.strip().split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        if not k:
            continue
        cookies.append({
            "name": k.strip(), "value": v.strip(),
            "domain": domain, "path": "/",
        })
    return json.dumps({"cookies": cookies, "origins": []})
