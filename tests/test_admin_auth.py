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
from app.models import AdminAccessToken, AdminUser


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


if __name__ == "__main__":
    unittest.main()