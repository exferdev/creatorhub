"""算法中心客户端: 直连 exferdev/js (js.faryi.com) 管理端点(Admin-Key)。

端点: /health /algorithms /metrics /admin/algorithm/switch
多 POP 遥测为趋势数据(非计费精度), 由 Console 侧累计历史样本做图。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class AlgoError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AlgoClient:
    def __init__(self, base_url: str, admin_key: str = "", timeout: float = 15.0,
                 transport: Any = None):
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout = timeout
        self.transport = transport

    def _headers(self, admin: bool = True) -> dict:
        h = {"Content-Type": "application/json"}
        if self.admin_key:
            h["X-Admin-Key"] = self.admin_key
        return h

    async def _request(self, method: str, path: str,
                       json: Any = None) -> Any:
        kwargs = {"base_url": self.base_url, "timeout": self.timeout,
                  "trust_env": False}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**kwargs) as cli:
                r = await cli.request(method, path, json=json,
                                      headers=self._headers())
        except httpx.HTTPError as e:
            raise AlgoError(f"算法服务不可达: {type(e).__name__}: {str(e)[:120]}")
        if r.status_code == 401:
            raise AlgoError("算法服务 401: CONSOLE_ALGO_ADMIN_KEY 无效或未配置", 401)
        if r.status_code >= 400:
            raise AlgoError(f"算法服务 HTTP {r.status_code}: {r.text[:200]}",
                            r.status_code)
        try:
            return r.json()
        except Exception:
            return None

    async def health(self) -> dict:
        data = await self._request("GET", "/health")
        return data if isinstance(data, dict) else {"ok": False}

    async def catalog(self) -> dict:
        data = await self._request("GET", "/algorithms")
        return data if isinstance(data, dict) else {"ok": False, "catalog": {}}

    async def metrics(self) -> dict:
        data = await self._request("GET", "/metrics")
        return data if isinstance(data, dict) else {"ok": False}

    async def switch(self, platform: str, algorithm: str, version: str) -> dict:
        return await self._request("POST", "/admin/algorithm/switch", json={
            "platform": platform, "algorithm": algorithm, "version": version})