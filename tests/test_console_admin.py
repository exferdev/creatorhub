"""Console 后台管理(starlette-admin)测试: 登录/视图/越权/数据浏览。

依赖 starlette-admin(仅 Console 独立环境安装); 主 venv(客户端)全量运行时跳过。
注意: admin 在 lifespan 中挂载一次并绑定当时引擎, 故本套件用 setUpClass 建库
(测试间不释放引擎), tearDownClass 统一释放 —— 保证单跑/合跑稳定。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pytest

starlette_admin = pytest.importorskip("starlette_admin", reason="Console 独立环境")

from fastapi.testclient import TestClient
from sqlmodel import select

import console.db as cdb
from console.console_auth import hash_password
from console.models import ClientAccount, ConsoleUser


class AdminUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        cls._prev_db = os.environ.get("CONSOLE_DB_PATH")
        cls.db_path = str(Path(cls.tmp) / "console.db")
        os.environ["CONSOLE_DB_PATH"] = cls.db_path
        cdb.init_db(cls.db_path)

        with cdb.get_session() as s:
            s.add(ConsoleUser(username="boss", email="boss@c", role="admin",
                              is_superuser=True,
                              hashed_password=hash_password("boss-pass-1")))
            s.add(ConsoleUser(username="op", email="op@c", role="operator",
                              hashed_password=hash_password("op-pass-123")))
            s.add(ConsoleUser(username="vw", email="vw@c", role="viewer",
                              hashed_password=hash_password("vw-pass-123")))
            s.add(ClientAccount(username="client-a",
                                password_hash=hash_password("cp-1"),
                                client_token="tok-1"))
            s.add(ClientAccount(username="client-b",
                                password_hash=hash_password("cp-2"),
                                client_token="tok-2", disabled=True))
            s.commit()
        # 每类 reload console.main 获得全新 app(避免跨类共享已挂载的 admin,
        # mount 同路径是追加, 旧 admin_app 会优先路由到遗留库)
        import importlib
        import console.main as cm
        cls.app = importlib.reload(cm).app

    @classmethod
    def tearDownClass(cls):
        if cls._prev is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = cls._prev
        if cls._prev_db is None:
            os.environ.pop("CONSOLE_DB_PATH", None)
        else:
            os.environ["CONSOLE_DB_PATH"] = cls._prev_db
        if cdb._engine is not None:
            try:
                cdb._engine.dispose()
            except Exception:
                pass
            cdb._engine = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client(self):
        return TestClient(self.app)

    def _login_admin(self, client, username="boss", password="boss-pass-1"):
        return client.post("/admin/login", data={"username": username,
                                                 "password": password},
                           follow_redirects=False)

    def test_login_page_and_success(self):
        with self._client() as client:
            r = client.get("/admin/login")
            self.assertEqual(r.status_code, 200)
            self.assertIn("username", r.text.lower())
            r = self._login_admin(client)
            self.assertIn(r.status_code, (303, 302), r.text[:200])
            # 已登录可访问后台首页
            r = client.get("/admin")
            self.assertEqual(r.status_code, 200)
            self.assertIn("CreatorHub Console", r.text)

    def test_wrong_password_400_viewer_forbidden(self):
        with self._client() as client:
            r = client.post("/admin/login",
                            data={"username": "boss", "password": "bad"},
                            follow_redirects=False)
            self.assertEqual(r.status_code, 400)
            # viewer 角色不可登录后台
            r = client.post("/admin/login",
                            data={"username": "vw", "password": "vw-pass-123"},
                            follow_redirects=False)
            self.assertEqual(r.status_code, 400)
            # operator 可登录
            r = client.post("/admin/login",
                            data={"username": "op", "password": "op-pass-123"},
                            follow_redirects=False)
            self.assertIn(r.status_code, (303, 302), r.text[:200])

    def test_client_list_view_shows_data(self):
        with self._client() as client:
            self._login_admin(client)
            r = client.get("/admin/client/list")
            self.assertEqual(r.status_code, 200, r.text[:200])
            # 数据由 DataTables 经 /admin/api/client 异步加载(items 结构)
            r = client.get("/admin/api/client")
            self.assertEqual(r.status_code, 200, r.text[:200])
            data = r.json()
            items = data.get("items", [])
            self.assertEqual(len(items), 2, r.text[:300])
            names = [it.get("username") for it in items]
            self.assertIn("client-a", names)
            self.assertIn("client-b", names)

    def test_audit_and_command_views(self):
        with self._client() as client:
            self._login_admin(client)
            for path in ("/admin/clientaudit/list", "/admin/clientcmd/list",
                         "/admin/consoleuser/list", "/admin/consoleaudit/list"):
                r = client.get(path)
                self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
            self.assertIn("boss", client.get(
                "/admin/consoleuser/list").text)

    def test_dashboard_guide_password_pages(self):
        with self._client() as client:
            self._login_admin(client)
            # 仪表盘: 统计卡片 + 操作审计动态
            r = client.get("/admin/")
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIn("客户端总数", r.text)
            self.assertIn("待办指令", r.text)
            # 接入指引
            r = client.get("/admin/guide")
            self.assertEqual(r.status_code, 200)
            self.assertIn("config.yaml", r.text)
            # 改密页修改的是"当前登录用户"; 先切到 op 再改(测完改回)
            r = client.post("/admin/login",
                            data={"username": "op", "password": "op-pass-123"},
                            follow_redirects=False)
            self.assertIn(r.status_code, (303, 302), r.text[:200])
            r = client.post("/admin/password",
                            data={"current_password": "wrong",
                                  "new_password": "op-pass-2",
                                  "confirm_password": "op-pass-2"})
            self.assertIn("当前密码不正确", r.text)
            r = client.post("/admin/password",
                            data={"current_password": "op-pass-123",
                                  "new_password": "op-pass-2",
                                  "confirm_password": "op-pass-2"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("密码已修改", r.text)
            # 新密码可登录
            r = client.post("/api/console/auth/login",
                            data={"username": "op",
                                  "password": "op-pass-2"})
            self.assertEqual(r.status_code, 200, r.text)
            # 改回, 保持其它用例稳定
            r = client.post("/admin/login",
                            data={"username": "op", "password": "op-pass-2"},
                            follow_redirects=False)
            client.post("/admin/password",
                        data={"current_password": "op-pass-2",
                              "new_password": "op-pass-123",
                              "confirm_password": "op-pass-123"})
            r = client.post("/api/console/auth/login",
                            data={"username": "op",
                                  "password": "op-pass-123"})
            self.assertEqual(r.status_code, 200, r.text)

    def test_row_actions_disable_enable_reset_risk(self):
        from console.models import ClientCommand, ConsoleAudit
        with self._client() as client:
            self._login_admin(client)
            # 停用
            r = client.post("/admin/api/client/row-action?pk=1&name=client_disable")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("已停用", r.json()["msg"])
            with cdb.get_session() as s:
                acc = s.get(ClientAccount, 1)
                self.assertTrue(acc.disabled)
            # verify 拒绝
            r = client.post("/api/clients/verify",
                            json={"username": "client-a",
                                  "password": "cp-1"})
            self.assertEqual(r.status_code, 403)
            # 重复停用 → 400
            r = client.post("/admin/api/client/row-action?pk=1&name=client_disable")
            self.assertEqual(r.status_code, 400)
            # 启用
            r = client.post("/admin/api/client/row-action?pk=1&name=client_enable")
            self.assertEqual(r.status_code, 200, r.text)
            # 重置密码(表单)
            r = client.post("/admin/api/client/row-action?pk=1&name=client_reset_password",
                            data={"new_password": "new-pw-123"})
            self.assertEqual(r.status_code, 200, r.text)
            r = client.post("/api/clients/verify",
                            json={"username": "client-a",
                                  "password": "new-pw-123"})
            self.assertEqual(r.status_code, 200)
            # 短密码 → 400
            r = client.post("/admin/api/client/row-action?pk=1&name=client_reset_password",
                            data={"new_password": "123"})
            self.assertEqual(r.status_code, 400)
            # 下发风控指令
            r = client.post("/admin/api/client/row-action?pk=1&name=client_send_risk",
                            data={"payload": '{"risk_control": {"enabled": true}}'})
            self.assertEqual(r.status_code, 200, r.text)
            with cdb.get_session() as s:
                cmds = s.exec(select(ClientCommand).where(
                    ClientCommand.client_name == "client-a")).all()
                self.assertEqual(len(cmds), 1)
                self.assertEqual(cmds[0].op, "risk.set")
                self.assertEqual(cmds[0].status, "pending")
            # 坏的 JSON → 400
            r = client.post("/admin/api/client/row-action?pk=1&name=client_send_risk",
                            data={"payload": "not-json"})
            self.assertEqual(r.status_code, 400)
            # 行内动作全部落下控制台审计
            with cdb.get_session() as s:
                actions = [a.action for a in s.exec(
                    select(ConsoleAudit)).all()]
            self.assertIn("client.disable", actions)
            self.assertIn("client.enable", actions)
            self.assertIn("client.password_reset", actions)
            self.assertIn("client.command", actions)


if __name__ == "__main__":
    unittest.main()