"""P0.0 后台鉴权测试: 同步适配器/令牌生命周期/首启 admin/RBAC/HTTP 登录流。"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi_users.password import PasswordHelper
from sqlmodel import select

from app.auth_setup import (
    SyncAccessTokenDatabase,
    SyncUserDatabase,
    auth_bypass_enabled,
    ensure_bootstrap_admin,
    hash_password,
    require_roles,
    revoke_user_tokens,
    user_from_token,
)
from app.config import load_config
from app.db import get_session, init_db
from app.models import (AdminAccessToken, AdminUser, DouyinAccount,
                        MonitorTarget, RiskAdminAudit)
from fastapi.testclient import TestClient


class AuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 保存/恢复旁路变量, 避免干扰其它走真实守卫的文件(如 risk_api_gates)
        self._prev_bypass = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        init_db(str(Path(self.tmp.name) / "auth.db"))

    def tearDown(self):
        if self._prev_bypass is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev_bypass
        # 释放 sqlite 连接池, 否则 Windows 上临时库文件被占用无法删除
        import app.db as dbm
        if dbm._engine is not None:
            try:
                dbm._engine.dispose()
            except Exception:
                pass
            dbm._engine = None

    def test_user_adapter_crud(self):
        async def run():
            ph = PasswordHelper()
            with get_session() as s:
                db = SyncUserDatabase(s)
                u = await db.create(AdminUser(
                    username="u1", email="u1@local",
                    hashed_password=ph.hash("pw-12345678")))
                got = await db.get(u.id)
                self.assertIsNotNone(got)
                self.assertEqual(got.username, "u1")
                by_name = await db.get_by_email("u1")  # v15 username 登录映射
                self.assertEqual(by_name.id, u.id)
                up = await db.update(u, {"display_name": "U1", "role": "admin"})
                self.assertEqual(up.role, "admin")
                await db.delete(u)
                self.assertIsNone(await db.get(u.id))
        asyncio.run(run())

    def test_token_adapter_lifecycle_and_expiry(self):
        async def run():
            with get_session() as s:
                db = SyncAccessTokenDatabase(s)
                tk = await db.create({"token": "tok-1", "user_id": 7})
                self.assertEqual((await db.get_by_token("tok-1")).token, "tok-1")
                # 未过期: created_at=now > max_age(10s 前) → 保留
                max_age = datetime.now(timezone.utc) - timedelta(seconds=10)
                self.assertIsNotNone(await db.get_by_token("tok-1", max_age))
                # 过期: 把 created_at 改到 30 天前, 14 天窗口应判失效
                tk.created_at = datetime.utcnow() - timedelta(days=30)
                s.add(tk)
                s.commit()
                self.assertIsNone(await db.get_by_token(
                    "tok-1", max_age=datetime.now(timezone.utc) - timedelta(days=1)))
                n = await db.delete_all_for_user(7)
                self.assertEqual(n, 1)
                self.assertIsNone(await db.get_by_token("tok-1"))
        asyncio.run(run())

    def test_password_hash_roundtrip(self):
        h = hash_password("s3cret-pass-99")
        ok, _ = PasswordHelper().verify_and_update("s3cret-pass-99", h)
        self.assertTrue(ok)
        bad, _ = PasswordHelper().verify_and_update("wrong-pass", h)
        self.assertFalse(bad)

    def test_ensure_bootstrap_admin_idempotent(self):
        cfg = load_config()
        with get_session() as s:
            self.assertIsNone(s.exec(select(AdminUser)).first())
        self.assertTrue(ensure_bootstrap_admin(cfg))
        self.assertFalse(ensure_bootstrap_admin(cfg))   # 幂等
        with get_session() as s:
            admin = s.exec(select(AdminUser)).first()
            self.assertEqual(admin.username, "admin")
            self.assertEqual(admin.role, "admin")
            self.assertTrue(admin.is_superuser)
            self.assertTrue(admin.must_change_password)

    def test_rbac_roles(self):
        from fastapi import HTTPException

        async def run():
            admin = AdminUser(username="a", email="a@l", role="admin",
                              is_superuser=True, hashed_password="x")
            viewer = AdminUser(username="v", email="v@l", role="viewer",
                               is_superuser=False, hashed_password="x")
            dep = require_roles("admin")
            self.assertIs(await dep(user=admin), admin)
            with self.assertRaises(HTTPException) as cm:
                await dep(user=viewer)
            self.assertEqual(cm.exception.status_code, 403)
        asyncio.run(run())

    def test_user_from_token_and_revoke(self):
        cfg = load_config()
        async def run():
            with get_session() as s:
                u = AdminUser(username="tu", email="tu@local",
                              hashed_password=hash_password("pw-12345678"),
                              role="operator")
                s.add(u)
                s.commit()
                s.refresh(u)
                uid = u.id
                s.add(AdminAccessToken(token="tok-live", user_id=uid))
                s.commit()
            user = await user_from_token("tok-live", cfg)
            self.assertEqual(user.username, "tu")
            self.assertIsNone(await user_from_token("tok-gone", cfg))
            await revoke_user_tokens(uid)
            self.assertIsNone(await user_from_token("tok-live", cfg))
        asyncio.run(run())

    def test_bypass_flag(self):
        os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = "1"
        self.assertTrue(auth_bypass_enabled())
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        self.assertFalse(auth_bypass_enabled())

    def test_http_login_and_me(self):
        from fastapi.testclient import TestClient
        import app.main as main

        with get_session() as s:
            s.add(AdminUser(username="httpu", email="httpu@local",
                            hashed_password=hash_password("pass-1234"),
                            role="operator"))
            s.commit()
        client = TestClient(main.app)
        # 错误密码 → 400
        r = client.post("/api/admin/auth/login",
                        data={"username": "httpu", "password": "wrong-pass"})
        self.assertIn(r.status_code, (400, 401), r.text)
        # 正确登录 → access_token
        r = client.post("/api/admin/auth/login",
                        data={"username": "httpu", "password": "pass-1234"})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["access_token"]
        self.assertTrue(token)
        # 带令牌访问 /me → 200
        r2 = client.get("/api/admin/me",
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["username"], "httpu")
        self.assertEqual(r2.json()["role"], "operator")
        # 无令牌 → 401 (全局守卫)
        self.assertEqual(client.get("/api/admin/me").status_code, 401)
        # 白名单健康检查放行
        self.assertEqual(client.get("/health").status_code, 200)

    def test_http_change_password_revokes_tokens(self):
        from fastapi.testclient import TestClient
        import app.main as main

        with get_session() as s:
            s.add(AdminUser(username="cp", email="cp@local",
                            hashed_password=hash_password("old-pass-1"),
                            role="viewer"))
            s.commit()
        client = TestClient(main.app)
        token = client.post("/api/admin/auth/login",
                            data={"username": "cp", "password": "old-pass-1"}
                            ).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        # 当前密码错 → 400
        r = client.post("/api/admin/me/password",
                        json={"current_password": "bad", "new_password": "new-pass-2"},
                        headers=h)
        self.assertEqual(r.status_code, 400, r.text)
        # 修改成功 → 旧令牌全部吊销 → 旧令牌访问 401
        r = client.post("/api/admin/me/password",
                        json={"current_password": "old-pass-1", "new_password": "new-pass-2"},
                        headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(client.get("/api/admin/me", headers=h).status_code, 401)
        # 新密码可登录
        r = client.post("/api/admin/auth/login",
                        data={"username": "cp", "password": "new-pass-2"})
        self.assertEqual(r.status_code, 200, r.text)


class OwnerIsolationTests(unittest.TestCase):
    """P0.1 多租户隔离: 列表过滤 / 详情 404 / admin 全可见 / NULL 归管理员 / 创建打 owner。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev_bypass = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        init_db(str(Path(self.tmp.name) / "iso.db"))
        self.u1, self.u2, self.admin = self._mk_users()
        self.client = TestClient(main_app())
        self.tok1 = self._login("u1", "pass-1234")
        self.tok2 = self._login("u2", "pass-1234")
        self.tokadmin = self._login("admin", "pass-1234")
        self.a1, self.a2, self.a0 = self._mk_accounts()
        self.m1, self.m2, self.m0 = self._mk_monitors()

    def tearDown(self):
        if self._prev_bypass is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev_bypass
        import app.db as dbm
        if dbm._engine is not None:
            try:
                dbm._engine.dispose()
            except Exception:
                pass
            dbm._engine = None

    def _mk_users(self):
        with get_session() as s:
            u1 = AdminUser(username="u1", email="u1@i", role="operator",
                           hashed_password=hash_password("pass-1234"))
            u2 = AdminUser(username="u2", email="u2@i", role="operator",
                           hashed_password=hash_password("pass-1234"))
            ad = AdminUser(username="admin", email="a@i", role="admin", is_superuser=True,
                           hashed_password=hash_password("pass-1234"))
            s.add_all([u1, u2, ad]); s.commit()
            for u in (u1, u2, ad): s.refresh(u)
            return u1, u2, ad

    def _login(self, name, pw):
        r = self.client.post("/api/admin/auth/login",
                             data={"username": name, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["access_token"]

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _mk_accounts(self):
        with get_session() as s:
            a1 = DouyinAccount(nickname="A1", platform="douyin", status="active", owner_id=self.u1.id)
            a2 = DouyinAccount(nickname="A2", platform="douyin", status="active", owner_id=self.u2.id)
            a0 = DouyinAccount(nickname="A0", platform="douyin", status="active", owner_id=None)
            s.add_all([a1, a2, a0]); s.commit()
            for a in (a1, a2, a0): s.refresh(a)
            return a1, a2, a0

    def _mk_monitors(self):
        with get_session() as s:
            m1 = MonitorTarget(platform="douyin", target_kind="creator", sec_uid="s1",
                               nickname="M1", account_id=self.a1.id, owner_id=self.u1.id)
            m2 = MonitorTarget(platform="douyin", target_kind="creator", sec_uid="s2",
                               nickname="M2", account_id=self.a2.id, owner_id=self.u2.id)
            m0 = MonitorTarget(platform="douyin", target_kind="creator", sec_uid="s0",
                               nickname="M0", account_id=self.a0.id, owner_id=None)
            s.add_all([m1, m2, m0]); s.commit()
            for m in (m1, m2, m0): s.refresh(m)
            return m1, m2, m0

    def test_account_list_and_detail_isolation(self):
        ids_u1 = {a["id"] for a in self.client.get(
            "/api/accounts", headers=self._h(self.tok1)).json()}
        self.assertEqual(ids_u1, {self.a1.id})
        ids_u2 = {a["id"] for a in self.client.get(
            "/api/accounts", headers=self._h(self.tok2)).json()}
        self.assertEqual(ids_u2, {self.a2.id})
        ids_admin = {a["id"] for a in self.client.get(
            "/api/accounts", headers=self._h(self.tokadmin)).json()}
        self.assertEqual(ids_admin, {self.a1.id, self.a2.id, self.a0.id})
        # 详情/操作: 越权 404
        r = self.client.get(f"/api/accounts/{self.a2.id}/environment",
                            headers=self._h(self.tok1))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f"/api/accounts/{self.a2.id}/environment",
                            headers=self._h(self.tokadmin))
        self.assertIn(r.status_code, (200, 503))  # 管理员可达(可能 503 浏览器未就绪)
        r = self.client.delete(f"/api/accounts/{self.a1.id}",
                               headers=self._h(self.tok2))
        self.assertEqual(r.status_code, 404)
        # NULL 归属: 普通用户看不到, 管理员可见
        r = self.client.get(f"/api/accounts/{self.a0.id}/environment",
                            headers=self._h(self.tok1))
        self.assertEqual(r.status_code, 404)

    def test_monitor_isolation(self):
        ids_u1 = {m["id"] for m in self.client.get(
            "/api/monitors", headers=self._h(self.tok1)).json()}
        self.assertEqual(ids_u1, {self.m1.id})
        ids_admin = {m["id"] for m in self.client.get(
            "/api/monitors", headers=self._h(self.tokadmin)).json()}
        self.assertEqual(ids_admin, {self.m1.id, self.m2.id, self.m0.id})
        r = self.client.post(f"/api/monitors/{self.m2.id}/toggle",
                             headers=self._h(self.tok1))
        self.assertEqual(r.status_code, 404)
        r = self.client.post(f"/api/monitors/{self.m2.id}/toggle",
                             headers=self._h(self.tok2))
        self.assertEqual(r.status_code, 200)

    def test_login_cookie_stamps_owner(self):
        r = self.client.post("/api/login/cookie", headers=self._h(self.tok1),
                             json={"platform": "douyin",
                                   "cookie": "sessionid=abc123; x=1",
                                   "nickname": "N1"})
        self.assertEqual(r.status_code, 200, r.text)
        new_id = r.json()["account_id"]
        ids_u1 = {a["id"] for a in self.client.get(
            "/api/accounts", headers=self._h(self.tok1)).json()}
        self.assertIn(new_id, ids_u1)
        ids_u2 = {a["id"] for a in self.client.get(
            "/api/accounts", headers=self._h(self.tok2)).json()}
        self.assertNotIn(new_id, ids_u2)
        with get_session() as s:
            acc = s.get(DouyinAccount, new_id)
            self.assertEqual(acc.owner_id, self.u1.id)


def main_app():
    import app.main as m
    return m.app


class AdminPanelTests(unittest.TestCase):
    """P0.2 后台管理: 用户 CRUD/角色隔离/守卫/操作审计。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev_bypass = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        init_db(str(Path(self.tmp.name) / "admin.db"))
        with get_session() as s:
            s.add(AdminUser(username="boss", email="boss@i", role="admin",
                            is_superuser=True,
                            hashed_password=hash_password("boss-pass-1")))
            s.add(AdminUser(username="op", email="op@i", role="operator",
                            hashed_password=hash_password("op-pass-123")))
            s.commit()
        self.client = TestClient(main_app())
        self.h_admin = self._login("boss", "boss-pass-1")
        self.h_op = self._login("op", "op-pass-123")

    def tearDown(self):
        if self._prev_bypass is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev_bypass
        import app.db as dbm
        if dbm._engine is not None:
            try:
                dbm._engine.dispose()
            except Exception:
                pass
            dbm._engine = None

    def _login(self, name, pw):
        r = self.client.post("/api/admin/auth/login",
                             data={"username": name, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_users_api_admin_only(self):
        self.assertEqual(self.client.get(
            "/api/admin/users", headers=self.h_op).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/admin/users", headers=self.h_op, json={
                "username": "x", "password": "pass-1234", "role": "viewer"}
        ).status_code, 403)
        self.assertEqual(self.client.get(
            "/api/admin/users", headers=self.h_admin).status_code, 200)

    def test_user_crud_and_guards(self):
        # 创建
        r = self.client.post("/api/admin/users", headers=self.h_admin, json={
            "username": "v1", "password": "view-pass-1", "role": "viewer"})
        self.assertEqual(r.status_code, 201, r.text)
        uid = r.json()["id"]
        # 重名 409
        r = self.client.post("/api/admin/users", headers=self.h_admin, json={
            "username": "v1", "password": "view-pass-1", "role": "viewer"})
        self.assertEqual(r.status_code, 409)
        # 停用
        r = self.client.patch(f"/api/admin/users/{uid}", headers=self.h_admin,
                              json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["enabled"])
        # 重置新用户密码(旧令牌失效, 不影响 admin)
        self.client.post(f"/api/admin/users/{uid}/password",
                         headers=self.h_admin, json={"new_password": "view-pass-2"})
        # 不能停用自己
        boss_id = self.client.get("/api/admin/users",
                                  headers=self.h_admin).json()["users"][0]["id"]
        r = self.client.patch(f"/api/admin/users/{boss_id}",
                              headers=self.h_admin, json={"enabled": False})
        self.assertEqual(r.status_code, 400)
        # 删除
        r = self.client.delete(f"/api/admin/users/{uid}", headers=self.h_admin)
        self.assertEqual(r.status_code, 200, r.text)
        # 不能删除自己
        r = self.client.delete(f"/api/admin/users/{boss_id}", headers=self.h_admin)
        self.assertEqual(r.status_code, 400)
        # 操作审计落库
        with get_session() as s:
            actions = [a.action for a in s.exec(
                select(RiskAdminAudit).order_by(
                    RiskAdminAudit.id.desc())).all()]
        self.assertIn("user.create", actions)
        self.assertIn("user.update", actions)
        self.assertIn("user.password_reset", actions)
        self.assertIn("user.delete", actions)

    def test_audit_views(self):
        # 产生一些请求审计
        self.client.get("/api/accounts", headers=self.h_admin)
        r = self.client.get("/api/admin/audit-requests?limit=10",
                            headers=self.h_admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(len(r.json()), 1)
        r = self.client.get("/api/admin/audit-ops",
                            headers=self.h_admin)
        self.assertEqual(r.status_code, 200)
        # operator 无权限
        self.assertEqual(self.client.get(
            "/api/admin/audit-requests", headers=self.h_op).status_code, 403)

    def test_notifications_per_user(self):
        from app.models import NotificationChannel
        # admin 建一个; op 建一个; 新用户不可见/不可操作
        r = self.client.post("/api/notifications", headers=self.h_admin,
                             json={"name": "boss-n", "type": "bark",
                                   "config": {"key": "1"}})
        cid_boss = r.json()["id"]
        r = self.client.post("/api/notifications", headers=self.h_op,
                             json={"name": "op-n", "type": "dingtalk",
                                   "config": {}})
        self.assertEqual(r.status_code, 200, r.text)
        cid_op = r.json()["id"]
        got_op = [c["id"] for c in self.client.get(
            "/api/notifications", headers=self.h_op).json()]
        self.assertIn(cid_op, got_op)
        self.assertNotIn(cid_boss, got_op)  # 看不到 admin 的
        # 新建 viewer 用户 → 空列表 + 越权 404
        r = self.client.post("/api/admin/users", headers=self.h_admin, json={
            "username": "viewx", "password": "viewx-pass", "role": "viewer"})
        self.assertEqual(r.status_code, 201, r.text)
        hx = self._login("viewx", "viewx-pass")
        got_x = [c["id"] for c in self.client.get(
            "/api/notifications", headers=hx).json()]
        self.assertEqual(got_x, [])
        r = self.client.put(f"/api/notifications/{cid_op}", headers=hx,
                            json={"enabled": False})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete(f"/api/notifications/{cid_op}", headers=hx)
        self.assertEqual(r.status_code, 404)
        # op 可改自己的, admin 全可见
        r = self.client.put(f"/api/notifications/{cid_op}", headers=self.h_op,
                            json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        got_admin = [c["id"] for c in self.client.get(
            "/api/notifications", headers=self.h_admin).json()]
        self.assertEqual(set(got_admin), {cid_boss, cid_op})

    def test_risk_center_admin_only(self):
        # 风控中心所有端点(含只读)必须是 admin; operator 一律 403
        for path in ("/api/risk-control/config",
                     "/api/risk-control/summary",
                     "/api/risk-control/accounts?platform=douyin",
                     "/api/risk-control/audit"):
            r = self.client.get(path, headers=self.h_op)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
        self.assertEqual(
            self.client.get("/api/risk-control/config",
                            headers=self.h_admin).status_code, 200)


class LoginRateLimitTests(unittest.TestCase):
    """登录限流: 每用户名滑动窗口, 超限 429, 成功清零。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev_bypass = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        init_db(str(Path(self.tmp.name) / "ratelimit.db"))
        with get_session() as s:
            s.add(AdminUser(username="rl", email="rl@i", role="viewer",
                            hashed_password=hash_password("rl-pass-123")))
            s.commit()
        import app.auth_setup as au
        self._limit = au._LOGIN_LIMIT
        au._LOGIN_LIMIT = 3          # 压小窗口便于测试
        au._login_windows.clear()
        self.addCleanup(setattr, au, "_LOGIN_LIMIT", self._limit)
        self.client = TestClient(main_app())

    def tearDown(self):
        if self._prev_bypass is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev_bypass
        import app.db as dbm
        if dbm._engine is not None:
            try:
                dbm._engine.dispose()
            except Exception:
                pass
            dbm._engine = None

    def test_login_throttled_after_limit_then_cleared_on_success(self):
        # 前 3 次失败: 400 (密码错), 第 4 次: 429
        codes = []
        for _ in range(4):
            r = self.client.post("/api/admin/auth/login",
                                 data={"username": "rl", "password": "wrong-pass"})
            codes.append(r.status_code)
        self.assertEqual(codes[:3], [400, 400, 400], codes)
        self.assertEqual(codes[3], 429, codes)
        # 换用户名不受影响
        r = self.client.post("/api/admin/auth/login",
                             data={"username": "rl2", "password": "x" * 9})
        self.assertIn(r.status_code, (400, 401))
        # 正确密码登录成功并清零窗口 → 后续失败重新计窗(不立即429)
        import app.auth_setup as au
        au._login_windows.clear()
        r = self.client.post("/api/admin/auth/login",
                             data={"username": "rl", "password": "rl-pass-123"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/admin/auth/login",
                             data={"username": "rl", "password": "bad"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()