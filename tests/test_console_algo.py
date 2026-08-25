"""算法中心测试: /api/algo/* 代理 + /admin/algo 页面(假算法服务 transport)。"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

starlette_admin = pytest.importorskip("starlette_admin", reason="Console 独立环境")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import select

import console.db as cdb
from console.algo_client import AlgoClient
from console.console_auth import hash_password
from console.models import AlgoKey, AlgoMetricSample, ConsoleUser


class FakeAlgoTransport(httpx.AsyncBaseTransport):
    """内存假算法服务(js.faryi.com 管理端点)。"""

    def __init__(self):
        self.switched = []
        self.catalog = {"douyin": {
            "abogus": {"versions": ["v1", "v2"], "current": "v1"}}}

    async def handle_async_request(self, request):
        method, path = request.method, request.url.path
        if method == "GET" and path == "/health":
            return httpx.Response(200, json={"ok": True, "platforms": ["douyin"],
                                             "algorithms": [{"name": "douyin/abogus",
                                                             "ok": True, "ms": 3}]})
        if method == "GET" and path == "/algorithms":
            return httpx.Response(200, json={"ok": True, "catalog": self.catalog})
        if method == "GET" and path == "/metrics":
            return httpx.Response(200, json={"ok": True,
                                             "algorithms": {"douyin/abogus": {
                                                 "count": 5, "errors": 0,
                                                 "rate_per_min": 5, "avg_ms": 3,
                                                 "p95_ms": 4}}})
        if method == "POST" and path == "/admin/algorithm/switch":
            body = json.loads(request.content or b"{}")
            self.switched.append(body)
            if body.get("version") not in ("v1", "v2"):
                return httpx.Response(404, json={"ok": False, "error": "版本不存在"})
            return httpx.Response(200, json={**body, "ok": True})
        return httpx.Response(404, json={"ok": False, "error": "no route"})

    async def handle_request(self, request):
        return await self.handle_async_request(request)


class AlgoCenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        cls._prev_db = os.environ.get("CONSOLE_DB_PATH")
        cls._prev_key = os.environ.get("CONSOLE_ALGO_ADMIN_KEY")
        cls._prev_url = os.environ.get("CONSOLE_ALGO_URL")
        os.environ["CONSOLE_DB_PATH"] = str(Path(cls.tmp) / "console.db")
        os.environ["CONSOLE_ALGO_ADMIN_KEY"] = "admin-secret"
        os.environ["CONSOLE_ALGO_URL"] = "http://fake-algo"
        cdb.init_db(os.environ["CONSOLE_DB_PATH"])

        with cdb.get_session() as s:
            s.add(ConsoleUser(username="boss", email="boss@c", role="admin",
                              is_superuser=True,
                              hashed_password=hash_password("boss-pass-1")))
            s.add(ConsoleUser(username="vw", email="vw@c", role="viewer",
                              hashed_password=hash_password("vw-pass-123")))
            s.commit()
        # 每类 reload 获得全新 app(避免跨类共享已挂载的 admin 路由到遗留库)
        import importlib
        import console.main as cm
        cls.cm = importlib.reload(cm)
        cls.app = cls.cm.app

    @classmethod
    def tearDownClass(cls):
        for var, prev in (("CREATORHUB_TEST_AUTH_BYPASS", cls._prev),
                          ("CONSOLE_DB_PATH", cls._prev_db),
                          ("CONSOLE_ALGO_ADMIN_KEY", cls._prev_key),
                          ("CONSOLE_ALGO_URL", cls._prev_url)):
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev
        if cdb._engine is not None:
            try:
                cdb._engine.dispose()
            except Exception:
                pass
            cdb._engine = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client_and_patch(self):
        fake = FakeAlgoTransport()
        p = patch.object(self.cm, "_algo_client",
                         lambda: AlgoClient("http://fake-algo", "admin-secret",
                                            transport=fake))
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _login(self, client, name="boss", pw="boss-pass-1"):
        r = client.post("/api/console/auth/login",
                        data={"username": name, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_api_status_catalog_metrics(self):
        fake = self._client_and_patch()
        with self._client() as client:
            h = self._login(client)
            r = client.get("/api/algo/status", headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(r.json()["health"]["algorithms"][0]["name"],
                             "douyin/abogus")
            r = client.get("/api/algo/catalog", headers=h)
            self.assertEqual(r.json()["catalog"]["douyin"]["abogus"]["current"],
                             "v1")
            r = client.get("/api/algo/metrics", headers=h)
            self.assertTrue(r.json()["ok"])
            # 历史样本落库
            with cdb.get_session() as s:
                rows = s.exec(select(AlgoMetricSample)).all()
            self.assertGreaterEqual(len(rows), 1)
            self.assertIn("douyin/abogus",
                          r.json()["history"][0]["payload"]["algorithms"])
            # viewer 只读可用
            hv = self._login(client, "vw", "vw-pass-123")
            self.assertEqual(client.get("/api/algo/status",
                                        headers=hv).status_code, 200)
            # viewer 不可 switch
            bad = client.post("/api/algo/switch", headers=hv, json={
                "platform": "douyin", "algorithm": "abogus", "version": "v2"})
            self.assertEqual(bad.status_code, 403)

    def test_api_switch_and_audit(self):
        fake = self._client_and_patch()
        with self._client() as client:
            h = self._login(client)
            r = client.post("/api/algo/switch", headers=h, json={
                "platform": "douyin", "algorithm": "abogus", "version": "v2"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["version"], "v2")
            self.assertEqual(fake.switched[-1]["version"], "v2")
            # 审计落库
            with cdb.get_session() as s:
                from console.models import ConsoleAudit
                actions = [a.action for a in s.exec(
                    select(ConsoleAudit)).all()]
            self.assertIn("algo.switch", actions)

    def test_api_keys(self):
        # pytest 按方法名字母序执行(admin_page 可能先建过 key), 先清空解耦
        with cdb.get_session() as s:
            for r in s.exec(select(AlgoKey)).all():
                s.delete(r)
            s.commit()
        self._client_and_patch()
        with self._client() as client:
            h = self._login(client)
            r = client.post("/api/algo/keys", headers=h, json={"name": "srv-1"})
            self.assertEqual(r.status_code, 201, r.text)
            key = r.json()["key_value"]
            self.assertGreater(len(key), 20)
            got = client.get("/api/algo/keys", headers=h).json()
            self.assertEqual(got[0]["name"], "srv-1")
            self.assertTrue(got[0]["key_prefix"].endswith("…"))
            # 数据库中明文保留(本地单机)
            with cdb.get_session() as s:
                row = s.exec(select(AlgoKey)).first()
                self.assertEqual(row.key_value, key)
            # 删除
            r = client.delete(f"/api/algo/keys/{got[0]['id']}", headers=h)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/api/algo/keys",
                                        headers=h).json(), [])

    def test_api_client_health(self):
        self._client_and_patch()
        with cdb.get_session() as s:
            from console.models import ClientAccount
            s.add(ClientAccount(username="c1", password_hash="ph",
                                client_token="t1",
                                status_json=json.dumps({
                                    "sign_health": {"douyin": {
                                        "total": 9, "ok_rate": 0.95,
                                        "errors": 1, "p95_ms": 12,
                                        "last_error": ""}}})))
            s.commit()
        with self._client() as client:
            h = self._login(client)
            r = client.get("/api/algo/client-health", headers=h)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["clients"][0]["client"], "c1")
            self.assertEqual(r.json()["clients"][0]["sign_health"]
                             ["douyin"]["total"], 9)

    def test_admin_page_loads_and_switch_form(self):
        fake = self._client_and_patch()
        with self._client() as client:
            client.post("/admin/login",
                        data={"username": "boss", "password": "boss-pass-1"})
            r = client.get("/admin/algo")
            self.assertEqual(r.status_code, 200, r.text[:300])
            self.assertIn("算法中心", r.text)
            self.assertIn("douyin", r.text)
            self.assertIn("v2", r.text)          # 注册表版本框
            self.assertIn("algo-chart", r.text)  # 图表容器
            # switch 表单(POST 到页面)
            r = client.post("/admin/algo", data={
                "action": "switch", "platform": "douyin",
                "algorithm": "abogus", "version": "v2"})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIn("已切换", r.text)
            self.assertEqual(fake.switched[-1]["version"], "v2")
            # 客户端命中健康表渲染(即使无数据也出表头)
            self.assertIn("客户端签名命中健康", r.text)
            # 密钥生成表单
            r = client.post("/admin/algo", data={
                "action": "key_create", "key_name": "srv-new"})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIn("新密钥(仅显示一次)", r.text)

    def test_client_poll_stores_metrics_and_data_page(self):
        """M1: 轮询 platform_stats → ClientMetric 落桶; 数据中心/仪表盘渲染。"""
        from console.models import ClientMetric
        with TestClient(self.app) as client:
            # 客户端注册 + 上报含 platform_stats
            tok = self.cm_client_token(client)
            r = client.post("/api/clients/poll", headers={
                "X-Client-Token": tok}, json={
                "status": {"version": "1.0", "platform_stats": [
                    {"platform": "douyin", "accounts": 3, "monitors": 2,
                     "works": 10, "comments": 40, "danmaku": 5,
                     "downloads": 1}]},
                "audit": [], "receipts": []})
            self.assertEqual(r.status_code, 200, r.text)
            with cdb.get_session() as s:
                rows = s.exec(select(ClientMetric)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].platform, "douyin")
            self.assertEqual(rows[0].accounts, 3)
            # 数据中心页: 矩阵 + 明细 + 趋势JSON
            client.post("/admin/login",
                        data={"username": "boss", "password": "boss-pass-1"})
            r = client.get("/admin/data")
            self.assertEqual(r.status_code, 200, r.text[:300])
            self.assertIn("平台总览", r.text)
            self.assertIn("douyin", r.text)
            self.assertIn("TREND =", r.text)
            # CSV 导出
            r = client.get("/admin/data?export=1")
            self.assertEqual(r.status_code, 200)
            self.assertIn("csv", r.headers.get(
                "content-type", ""), r.headers.get("content-type", ""))
            # 仪表盘: 跨平台合计卡
            r = client.get("/admin/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("账号合计", r.text)
            self.assertIn("平台摘要", r.text)

    def cm_client_token(self, client):
        r = client.post("/api/clients/register", json={
            "username": "dc1", "password": "dc-pass-1", "version": "1.0"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["client_token"]

    def _client(self):
        return TestClient(self.app)


if __name__ == "__main__":
    unittest.main()