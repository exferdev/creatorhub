"""CreatorHub 实例客户端: 用实例 admin 令牌调用其 /api/*(Bearer)。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

import httpx


class InstanceError(RuntimeError):
    """实例调用失败(含 HTTP 状态/实例意图标记)。"""

    def __init__(self, message: str, kind: str = "error", status: int = 0):
        super().__init__(message)
        self.kind = kind      # error | auth_expired | not_found | offline
        self.status = status


class InstanceClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 20.0,
                 transport: "httpx.AsyncBaseTransport | None" = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _request(self, method: str, path: str, *, json: Any = None,
                       form: dict | None = None) -> Any:
        kwargs = {"base_url": self.base_url, "timeout": self.timeout,
                  "trust_env": False}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            if form is not None:
                async with httpx.AsyncClient(**kwargs) as cli:
                    r = await cli.request(method, path, data=form,
                                          headers={"Content-Type":
                                                   "application/x-www-form-urlencoded"})
            else:
                async with httpx.AsyncClient(**kwargs) as cli:
                    r = await cli.request(method, path, json=json,
                                          headers=self._headers())
        except httpx.HTTPError as e:
            raise InstanceError(f"实例不可达: {type(e).__name__}: {str(e)[:120]}",
                                kind="offline") from e
        if r.status_code == 401:
            raise InstanceError("实例凭据过期或无效, 请重新授权", kind="auth_expired",
                                status=401)
        if r.status_code == 403:
            raise InstanceError("实例返回 403(权限不足)", kind="error", status=403)
        if r.status_code == 404:
            raise InstanceError("实例返回 404", kind="not_found", status=404)
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("detail") or body
            except Exception:
                detail = r.text[:200]
            raise InstanceError(f"实例返回 HTTP {r.status_code}: {detail}",
                                kind="error", status=r.status_code)
        try:
            return r.json()
        except Exception:
            return None

    # ── 实例侧 API ──
    async def auth_login(self, username: str, password: str) -> str:
        data = await self._request(
            "POST", "/api/admin/auth/login",
            form={"username": username, "password": password})
        token = (data or {}).get("access_token") or ""
        if not token:
            raise InstanceError("实例登录失败: 未返回 access_token",
                                kind="auth_expired")
        return token

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.base_url,
                                         timeout=8, trust_env=False) as cli:
                r = await cli.get("/health")
            return r.status_code == 200
        except Exception:
            return False

    async def list_accounts(self) -> list:
        data = await self._request("GET", "/api/accounts")
        return data if isinstance(data, list) else []

    async def list_monitors(self) -> list:
        data = await self._request("GET", "/api/monitors")
        return data if isinstance(data, list) else []

    async def list_users(self) -> dict:
        data = await self._request("GET", "/api/admin/users")
        return data if isinstance(data, dict) else {"count": 0, "users": []}

    async def create_user(self, username: str, password: str, role: str) -> dict:
        return await self._request("POST", "/api/admin/users",
                                   json={"username": username,
                                         "password": password, "role": role})

    async def patch_user(self, user_id: int, body: dict) -> dict:
        return await self._request("PATCH", f"/api/admin/users/{user_id}", json=body)

    async def reset_password(self, user_id: int, new_password: str) -> dict:
        return await self._request("POST", f"/api/admin/users/{user_id}/password",
                                   json={"new_password": new_password})

    async def delete_user(self, user_id: int) -> dict:
        return await self._request("DELETE", f"/api/admin/users/{user_id}")

    async def get_risk_config(self) -> dict:
        data = await self._request("GET", "/api/risk-control/config")
        return data if isinstance(data, dict) else {}

    async def put_risk_config(self, payload: dict) -> dict:
        return await self._request("PUT", "/api/risk-control/config", json=payload)

    async def audit_requests(self, limit: int = 100) -> list:
        data = await self._request("GET", f"/api/admin/audit-requests?limit={limit}")
        return data if isinstance(data, list) else []

    async def audit_ops(self, limit: int = 100) -> list:
        data = await self._request("GET", f"/api/admin/audit-ops?limit={limit}")
        return data if isinstance(data, list) else []

    @staticmethod
    def token_expired(expires_at) -> bool:
        if not expires_at:
            return True
        if isinstance(expires_at, datetime):
            return datetime.utcnow() > expires_at
        return True


def now_utc() -> datetime:
    return datetime.utcnow()


def _monotonic_seed() -> str:
    return str(time.time())