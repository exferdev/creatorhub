"""控制面(console)测试: 控制台鉴权/实例注册/远端代理/角色隔离/审计/令牌过期。"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlmodel import select

import console.db as cdb
from console.models import ConsoleAccessToken, ConsoleAudit, ConsoleInstance, ConsoleUser


def _req_admin_token_data():
    return {"access_token": "fake-instance-token", "token_type": "bearer"}


class FakeInstanceTransport(httpx.AsyncBaseTransport):
    """模拟一台 CreatorHub 实例(内存路由)。"""

    def __init__(self):
        self.users = [{"id": 1, "username": "admin", "role": "admin",
                       "enabled": True, "display_name": ""}]
        self.calls = []

    async def handle_async_request(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        if method == "POST" and path == "/api/admin/auth/login":
            return httpx.Response(200, json=_req_admin_token_data())
        if method == "GET" and path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if method == "GET" and path == "/api/accounts":
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/api/monitors":
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/api/admin/users":
            return httpx.Response(200, json={"count": len(self.users),
                                             "users": self.users})
        if method == "POST" and path == "/api/admin/users":
            body = json.loads(request.content or b"{}")
            uid = len(self.users) + 10
            self.users.append({"id": uid, "username": body.get("username"),
                               "role": body.get("role", "viewer"),
                               "enabled": True, "display_name": ""})
            return httpx.Response(201, json=self.users[-1])
        if method == "PATCH" and path.startswith("/api/admin/users/"):
            uid = int(path.rsplit("/", 1)[1])
            body = json.loads(request.content or b"{}")
            for u in self.users:
                if u["id"] == uid:
                    u.update(body)
                    return httpx.Response(200, json=u)
            return httpx.Response(404, json={"detail": "不存在"})
        if method == "DELETE" and path.startswith("/api/admin/users/"):
            uid = int(path.rsplit("/", 1)[1])
            self.users = [u for u in self.users if u["id"] != uid]
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and "/password" in path:
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and path == "/api/risk-control/config":
            return httpx.Response(200, json={"risk_control": {"enabled": True},
                                             "schedule": {}})
        if method == "PUT" and path == "/api/risk-control/config":
            return httpx.Response(200, json={"risk_control": {"enabled": True},
                                             "schedule": {}})
        if method == "GET" and "/audit-requests" in path:
            return httpx.Response(200, json=[])
        if method == "GET" and "/audit-ops" in path:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "no route"})

    async def handle_request(self, request):
        return await self.handle_async_request(request)


class ConsoleBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        cdb.init_db(str(Path(self.tmp.name) / "console.db"))
        from console.console_auth import hash_password
        with cdb.get_session() as s:
            s.add(ConsoleUser(username="boss", email="boss@c", role="admin",
                              is_superuser=True,
                              hashed_password=hash_password("boss-pass-1")))
            s.add(ConsoleUser(username="op", email="op@c", role="operator",
                              hashed_password=hash_password("op-pass-123")))
            s.commit()
        # 实例客户端的所有 httpx.AsyncClient 都注入假实例 transport
        self.fake = FakeInstanceTransport()
        from unittest.mock import patch
        fake = self.fake
        class _FakeAsyncClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw.setdefault("transport", fake)
                super().__init__(*a, **kw)
        self._cli_patch = patch.object(httpx, "AsyncClient", _FakeAsyncClient)
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)
        import console.main as cm
        self.app = cm.app
        self.client = TestClient(self.app)
        self.h = self._login("boss", "boss-pass-1")
        self.hop = self._login("op", "op-pass-123")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev
        if cdb._engine is not None:
            try:
                cdb._engine.dispose()
            except Exception:
                pass
            cdb._engine = None

    def _login(self, name, pw):
        r = self.client.post("/api/console/auth/login",
                             data={"username": name, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


class ConsoleAuthTests(ConsoleBaseTests):
    def test_login_and_change_password(self):
        r = self.client.get("/api/console/me", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["role"], "admin")
        # 改密 → 旧令牌失效
        r = self.client.post("/api/console/me/password", headers=self.h,
                             json={"current_password": "boss-pass-1",
                                   "new_password": "boss-pass-2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/console/me",
                                         headers=self.h).status_code, 401)
        self.h = self._login("boss", "boss-pass-2")

    def test_login_rate_limit(self):
        import console.console_auth as ca
        old = ca._LOGIN_LIMIT
        ca._LOGIN_LIMIT = 3
        ca._login_windows.clear()
        try:
            codes = []
            for _ in range(4):
                r = self.client.post("/api/console/auth/login",
                                     data={"username": "boss", "password": "bad"})
                codes.append(r.status_code)
            self.assertEqual(codes[:3], [400, 400, 400], codes)
            self.assertEqual(codes[3], 429, codes)
        finally:
            ca._LOGIN_LIMIT = old
            ca._login_windows.clear()


class ConsoleInstanceTests(ConsoleBaseTests):
    def _register(self, headers=None):
        headers = headers or self.h
        r = self.client.post("/api/instances", headers=headers, json={
            "name": "inst-a", "base_url": "http://fake:8000",
            "admin_username": "admin", "admin_password": "secret-pw",
        })
        return r

    def test_register_and_repeat(self):
        r = self._register()
        self.assertEqual(r.status_code, 201, r.text)
        iid = r.json()["id"]
        # 不落盘密码, 只存令牌
        with cdb.get_session() as s:
            inst = s.get(ConsoleInstance, iid)
            self.assertEqual(inst.token, "fake-instance-token")
            self.assertEqual(inst.admin_password if hasattr(inst, "admin_password") else "", "")
        # 重名/重地址 409
        r = self._register()
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/api/instances", headers=self.h, json={
            "name": "inst-b", "base_url": "http://fake:8000",
            "admin_username": "admin", "admin_password": "x"})
        self.assertEqual(r.status_code, 409)

    def test_remote_user_proxy_and_rbac(self):
        r = self._register()
        iid = r.json()["id"]
        # operator: 可见用户, 不可建号
        got = self.client.get(f"/api/instances/{iid}/users", headers=self.hop)
        self.assertEqual(got.status_code, 200, got.text)
        bad = self.client.post(f"/api/instances/{iid}/users", headers=self.hop,
                               json={"username": "x", "password": "pass-1234",
                                     "role": "viewer"})
        self.assertEqual(bad.status_code, 403)
        # admin 建号
        r = self.client.post(f"/api/instances/{iid}/users", headers=self.h,
                             json={"username": "remote1", "password": "pass-1234",
                                   "role": "viewer"})
        self.assertEqual(r.status_code, 201, r.text)
        uid = r.json()["id"]
        # 停用/重置/删除
        r = self.client.patch(f"/api/instances/{iid}/users/{uid}",
                              headers=self.h, json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/instances/{iid}/users/{uid}/password",
                             headers=self.h, json={"new_password": "new-pass-88"})
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/instances/{iid}/users/{uid}", headers=self.h)
        self.assertEqual(r.status_code, 200)

    def test_token_expired_requires_reauth(self):
        r = self._register()
        iid = r.json()["id"]
        with cdb.get_session() as s:
            inst = s.get(ConsoleInstance, iid)
            inst.token_expires_at = datetime.utcnow() - timedelta(minutes=1)
            s.add(inst)
            s.commit()
        r = self.client.get(f"/api/instances/{iid}/status", headers=self.h)
        self.assertEqual(r.status_code, 401, r.text)
        r = self.client.get(f"/api/instances/{iid}/users", headers=self.h)
        self.assertEqual(r.status_code, 401)

    def test_risk_get_put_rbac_and_audit(self):
        r = self._register()
        iid = r.json()["id"]
        got = self.client.get(f"/api/instances/{iid}/risk", headers=self.hop)
        self.assertEqual(got.status_code, 200, got.text)
        bad = self.client.put(f"/api/instances/{iid}/risk", headers=self.hop,
                              json={"risk_control": {"enabled": True}})
        self.assertEqual(bad.status_code, 403)
        ok = self.client.put(f"/api/instances/{iid}/risk", headers=self.h,
                             json={"risk_control": {"enabled": True}})
        self.assertEqual(ok.status_code, 200, ok.text)
        # 控制台审计落库(含本次操作)
        r = self.client.get("/api/console/audit", headers=self.h)
        self.assertEqual(r.status_code, 200)
        actions = [a["action"] for a in r.json()]
        self.assertIn("instance.add", actions)
        self.assertIn("risk.get", actions)
        self.assertIn("risk.put", actions)
        # operator 无权限看控制台审计
        self.assertEqual(self.client.get("/api/console/audit",
                                         headers=self.hop).status_code, 403)

    def test_audit_requests_proxy(self):
        r = self._register()
        iid = r.json()["id"]
        r = self.client.get(f"/api/instances/{iid}/audit-requests",
                            headers=self.hop)
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()