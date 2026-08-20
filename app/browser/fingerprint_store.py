"""fingerprint-db HTTP 客户端。

封装 fingerprint-db API 的只读数据消费 (ShardX Launcher 语义):
    list(platform) → 有序元数据列表
    load(name)     → 完整 profile JSON

数据主权: 指纹数据源**只从 fingerprint-db HTTP API (fingerprint_db_base_url) 获取,
无本地文件回退** (fingerprint_db_dir 已彻底移除)。API 未配置或不可达时直接抛
RuntimeError, 由调用方给出明确提示, 不再回退任何本地指纹库数据。
"""
from __future__ import annotations

import sys
from typing import Optional

import httpx


def host_platform_key() -> str:
    """宿主 OS → fingerprint-db API 平台过滤 key (对齐 deps._PLATFORM_ALIASES)。"""
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def resolve_navigator(client, *, shardx_id: str = "",
                      fingerprint_name: str = "", fp_seed: str = "",
                      platform: str = "") -> Optional[dict]:
    """解析账号将使用的指纹 profile, 返回其 navigator(只读, 不启动浏览器)。

    与浏览器侧同序: 绑定 shardx_id(账号自己的 ShardX 持久化 profile.json) >
    指定 fingerprint_name(指纹库) > fp_seed 确定性选择(指纹库)。

    - shardx_id 路径读账号自己固化的持久化 profile(属账号数据, 非指纹库, 不依赖 API);
    - 指纹库 (fingerprint_name / fp_seed) 只走 fingerprint-db HTTP API。
    API 未配置或不可达时抛 RuntimeError, 由调用方明确提示 (无本地回退)。
    """
    if shardx_id:
        try:
            from shardx import ShardX
            prof = ShardX().open_profile(shardx_id)
            nav = prof.config.get("navigator") if prof and getattr(prof, "config", None) else None
            if isinstance(nav, dict) and nav:
                return nav
        except Exception:
            pass
    if not (fingerprint_name or fp_seed):
        return None
    if client is None or not getattr(client, "enabled", False):
        raise RuntimeError(
            "fingerprint-db API 未配置 (fingerprint_db_base_url 为空), 且已无本地指纹库回退")
    try:
        if fingerprint_name:
            cfg = client.load_profile_sync(fingerprint_name)
        else:
            profiles = client.list_profiles_sync(platform or host_platform_key())
            if not profiles:
                return None
            idx = _seed_from(fp_seed) % len(profiles)
            cfg = client.load_profile_sync(profiles[idx]["name"])
        nav = (cfg or {}).get("navigator")
        if isinstance(nav, dict) and nav:
            return nav
        return None
    except RuntimeError:
        raise
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"fingerprint-db API 不可达: HTTP {e.response.status_code}") from e
    except Exception as e:
        raise RuntimeError(f"fingerprint-db API 不可达: {e}") from e


class FingerprintDbClient:
    """fingerprint-db 只读/写 API 客户端。

    list_profiles / load_profile / pick / randomize / submit_raw 为异步方法;
    list_profiles_sync / load_profile_sync 为同步版本 (strData 等非异步链路用)。
    """

    def __init__(self, base_url: str, read_key: str = "",
                 write_key: str = "", timeout: float = 15.0):
        self._base_url = (base_url or "").rstrip("/")
        self._read_key = read_key
        self._write_key = write_key
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    @property
    def write_enabled(self) -> bool:
        return bool(self._base_url) and bool(self._write_key)

    def _headers(self) -> dict:
        if self._read_key:
            return {"Authorization": f"Bearer {self._read_key}"}
        return {}

    def _write_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._write_key}"}

    # ── 同步版 (非异步链路: ms_token/strData 解析) ──
    def list_profiles_sync(self, platform: str) -> list[dict]:
        """同步: 返回**有序** profile 元数据列表。"""
        if not self.enabled:
            raise RuntimeError(
                "fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
        with httpx.Client(base_url=self._base_url, headers=self._headers(),
                          timeout=self._timeout, trust_env=False) as cli:
            r = cli.get("/api/v1/profiles/list", params={"platform": platform})
            r.raise_for_status()
            body = r.json()
            profiles = body.get("profiles", [])
            if not isinstance(profiles, list):
                raise RuntimeError(f"fingerprint-db list 返回异常: {body!r}")
            return profiles

    def load_profile_sync(self, name: str) -> dict:
        """同步: 返回单个 profile 完整 JSON。不存在抛 httpx.HTTPStatusError(404)。"""
        if not self.enabled:
            raise RuntimeError(
                "fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
        with httpx.Client(base_url=self._base_url, headers=self._headers(),
                          timeout=self._timeout, trust_env=False) as cli:
            r = cli.get(f"/api/v1/profiles/{name}")
            r.raise_for_status()
            return r.json()

    # ── 异步版 (浏览器/接口链路) ──
    async def list_profiles(self, platform: str) -> list[dict]:
        """返回**有序** profile 元数据列表 (name/platform/gpu/chrome_major/renderer)。"""
        if not self.enabled:
            raise RuntimeError(
                "fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
        async with httpx.AsyncClient(base_url=self._base_url,
                                     headers=self._headers(),
                                     timeout=self._timeout,
                                     trust_env=False) as cli:
            r = await cli.get("/api/v1/profiles/list",
                              params={"platform": platform})
            r.raise_for_status()
            body = r.json()
            profiles = body.get("profiles", [])
            if not isinstance(profiles, list):
                raise RuntimeError(f"fingerprint-db list 返回异常: {body!r}")
            return profiles

    async def load_profile(self, name: str) -> dict:
        """返回单个 profile 完整 JSON。不存在抛 httpx.HTTPStatusError(404)。"""
        if not self.enabled:
            raise RuntimeError(
                "fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
        async with httpx.AsyncClient(base_url=self._base_url,
                                     headers=self._headers(),
                                     timeout=self._timeout,
                                     trust_env=False) as cli:
            r = await cli.get(f"/api/v1/profiles/{name}")
            r.raise_for_status()
            return r.json()

    async def pick(self, fp_seed: str, platform: str) -> Optional[dict]:
        """确定性选择: 有序列表 + fp_seed 取模 + 取完整 JSON。

        复刻 Launcher 唯一实现 (同 fingerprint-db validate/launcher_integrate_test.py):
            idx = seed_from(fp_seed) % len(profiles)  →  GET /profiles/{name}
        返回完整 profile dict; 失败返回 None (调用方负责明确报错, 无本地回退)。
        """
        try:
            profiles = await self.list_profiles(platform)
        except Exception:
            return None
        if not profiles:
            return None
        idx = _seed_from(fp_seed) % len(profiles)
        name = profiles[idx]["name"]
        try:
            return await self.load_profile(name)
        except Exception:
            return None

    async def randomize(self, name: str) -> Optional[dict]:
        """加载指定 fingerprint 并随机化硬件/平台版本 (不持久化)。

        复刻 ShardX Launcher /fingerprint/new 语义: 基于一个库 profile,
        重新随机 hardware_concurrency/device_memory/platform_version,
        返回新的 config dict。改 GPU 后重随机配套硬件用。
        """
        cfg = await self.load_profile(name)
        if cfg is None:
            return None
        try:
            from shardx import randomize_hardware, randomize_platform_version
            randomize_hardware(cfg)
            randomize_platform_version(cfg)
        except Exception:
            pass
        return cfg

    async def submit_raw(self, name: str, data: dict) -> dict:
        """提交真机采集样本到 fingerprint-db (POST /api/v1/collect/raw)。

        需写 token (fingerprint_db_write_key)。指纹库侧做脏样本拒收,
        通过后存 database/real/<name>.json。返回 fingerprint-db 响应 dict。
        """
        if not self.write_enabled:
            raise RuntimeError(
                "fingerprint-db 写 token 未配置 (fingerprint_db_write_key 为空)")
        async with httpx.AsyncClient(base_url=self._base_url,
                                     headers=self._write_headers(),
                                     timeout=self._timeout,
                                     trust_env=False) as cli:
            r = await cli.post("/api/v1/collect/raw",
                               json={"name": name, "data": data})
            r.raise_for_status()
            return r.json()


def _seed_from(fp_seed: str) -> int:
    """fp_seed(hex) → 确定性整数种子 (复刻 Launcher/CreatorHub _fingerprint_seed_from)。"""
    try:
        return int(fp_seed, 16) % 2 ** 32
    except (ValueError, TypeError):
        return 10000 + (abs(hash(fp_seed or "")) % 90000)