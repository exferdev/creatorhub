"""fingerprint-db HTTP 客户端。

封装 fingerprint-db API 的只读数据消费 (ShardX Launcher 语义):
    list(platform) → 有序元数据列表
    load(name)      → 完整 profile JSON

确定性选择 (fp_seed) 由调用方实现 (Launcher 唯一实现), 本模块只提供
有序列表 + 单取。失败时抛异常, 由调用方决定回退文件直读。
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


class FingerprintDbClient:
    """fingerprint-db 只读 API 客户端。"""

    def __init__(self, base_url: str, read_key: str = "",
                 timeout: float = 15.0):
        self._base_url = (base_url or "").rstrip("/")
        self._read_key = read_key
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    def _headers(self) -> dict:
        if self._read_key:
            return {"Authorization": f"Bearer {self._read_key}"}
        return {}

    async def list_profiles(self, platform: str) -> list[dict]:
        """返回**有序** profile 元数据列表 (name/platform/gpu/chrome_major/renderer)。"""
        if not self.enabled:
            raise RuntimeError("fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
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
            raise RuntimeError("fingerprint-db API 未配置 (fingerprint_db_base_url 为空)")
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
        返回完整 profile dict, 失败返回 None (调用方回退)。
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


def _seed_from(fp_seed: str) -> int:
    """fp_seed(hex) → 确定性整数种子 (复刻 Launcher/CreatorHub _fingerprint_seed_from)。"""
    try:
        return int(fp_seed, 16) % 2 ** 32
    except (ValueError, TypeError):
        return 10000 + (abs(hash(fp_seed or "")) % 90000)
