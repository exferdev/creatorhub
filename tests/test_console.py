"""控制面测试: 客户端注册/轮询/验证/启停/指令下发与回执/审计/RBAC。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import select

import console.db as cdb
from console.console_auth import hash_password
from console.models import (ClientAccount, ClientAudit, ClientCommand,
                            ConsoleAudit, ConsoleUser)


class ConsoleSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        cdb.init_db(str(Path(self.tmp.name) / "console.db"))
        with cdb.get_session() as s:
            s.add(ConsoleUser(username="boss", email="boss@c", role="admin",
                              is_superuser=True,
                              hashed_password=hash_password("boss-pass-1")))
            s.add(ConsoleUser(username="op", email="op@c", role="operator",
                              hashed_password=hash_password("op-pass-123")))
            s.add(ConsoleUser(username="vw", email="vw@c", role="viewer",
                              hashed_password=hash_password("vw-pass-123")))
            s.commit()
        import console.main as cm
        self.app = cm.app
        self.client = TestClient(self.app)
        self.h = self._login("boss", "boss-pass-1")
        self.hop = self._login("op", "op-pass-123")
        self.hvw = self._login("vw", "vw-pass-123")
        self.token = self._register("client-a", "client-pass-1")

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

    def _register(self, username, password):
        r = self.client.post("/api/clients/register", json={
            "username": username, "password": password, "version": "1.0"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["client_token"]

    def _poll(self, token, body=None):
        return self.client.post("/api/clients/poll", headers={
            "X-Client-Token": token}, json=body or {"status": {}, "audit": [],
                                                    "receipts": []})

    def test_register_and_reject_wrong_password(self):
        # 重复注册: 密码错 → 401; 正确 → ok 且令牌稳定
        r = self.client.post("/api/clients/register", json={
            "username": "client-a", "password": "wrong-pass", "version": "1.0"})
        self.assertEqual(r.status_code, 401)
        r2 = self._register("client-a", "client-pass-1")
        self.assertEqual(r2, self.token)

    def test_poll_and_verify(self):
        # 轮询需令牌
        self.assertEqual(self.client.post("/api/clients/poll", json={}).
                         status_code, 401)
        bad = self.client.post("/api/clients/poll", headers={
            "X-Client-Token": "nope"}, json={})
        self.assertEqual(bad.status_code, 401)
        # 正常轮询: 无待办
        r = self._poll(self.token, {"status": {"accounts": 3, "monitors": 2},
                                    "audit": [{"kind": "request",
                                               "action": "GET /api/accounts",
                                               "username": "admin", "ok": True}],
                                    "receipts": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["commands"], [])
        # 状态与审计入库
        with cdb.get_session() as s:
            acc = s.exec(select(ClientAccount).where(
                ClientAccount.username == "client-a")).first()
            self.assertIn("3", acc.status_json)
            self.assertIsNotNone(acc.last_seen_at)
            aud = s.exec(select(ClientAudit)).all()
            self.assertEqual(len(aud), 1)
            self.assertEqual(aud[0].action, "GET /api/accounts")
        # verify
        self.assertEqual(self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "client-pass-1"}).status_code, 200)
        r = self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "bad-pass"})
        self.assertEqual(r.status_code, 401)

    def test_disable_enable_and_reenable_verify(self):
        r = self.client.post("/api/admin/clients/client-a/disable",
                             headers=self.hop)
        self.assertEqual(r.status_code, 200, r.text)
        # 停用: 验证 403, 轮询返回 disabled
        r = self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "client-pass-1"})
        self.assertEqual(r.status_code, 403)
        r = self._poll(self.token)
        self.assertEqual(r.json()["disabled"], True)
        # 启用: 恢复
        r = self.client.post("/api/admin/clients/client-a/enable", headers=self.hop)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "client-pass-1"})
        self.assertEqual(r.status_code, 200)
        r = self._poll(self.token)
        self.assertEqual(r.json()["disabled"], False)

    def test_command_flow(self):
        # 下发 risk.set
        r = self.client.post("/api/admin/clients/client-a/command",
                             headers=self.hop, json={
                                 "op": "risk.set",
                                 "params": {"risk_control": {"enabled": True}}})
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["command_id"]
        # 轮询取走指令 → 回执 done
        r = self._poll(self.token)
        cmds = r.json()["commands"]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["id"], cid)
        self.assertEqual(cmds[0]["op"], "risk.set")
        r = self._poll(self.token, {"status": {}, "audit": [],
                                    "receipts": [{"command_id": cid,
                                                  "status": "done",
                                                  "result": "ok"}]})
        # 指令已 done, 不再下发; commands 接口可见
        r = self._poll(self.token)
        self.assertEqual(r.json()["commands"], [])
        with cdb.get_session() as s:
            cmd = s.get(ClientCommand, cid)
            self.assertEqual(cmd.status, "done")
            self.assertEqual(cmd.result, "ok")

    def test_reset_password(self):
        r = self.client.post("/api/admin/clients/client-a/reset-password",
                             headers=self.hop, json={"new_password": "new-pass-66"})
        self.assertEqual(r.status_code, 200, r.text)
        # 旧密码失效, 新密码可验证
        self.assertEqual(self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "client-pass-1"}).status_code, 401)
        self.assertEqual(self.client.post("/api/clients/verify", json={
            "username": "client-a", "password": "new-pass-66"}).status_code, 200)

    def test_rbac_and_console_audit(self):
        # viewer 不可管理
        self.assertEqual(self.client.post(
            "/api/admin/clients/client-a/disable", headers=self.hvw).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/admin/clients/client-a/command", headers=self.hvw, json={
                "op": "risk.set", "params": {}}).status_code, 403)
        # viewer 可读列表/详情
        self.assertEqual(self.client.get("/api/admin/clients",
                                         headers=self.hvw).status_code, 200)
        # 成功执行一次停用 → 控制台审计落库
        self.assertEqual(self.client.post(
            "/api/admin/clients/client-a/disable", headers=self.hop).status_code, 200)
        with cdb.get_session() as s:
            rows = s.exec(select(ConsoleAudit)).all()
        self.assertIn("client.disable", [a.action for a in rows])


if __name__ == "__main__":
    unittest.main()