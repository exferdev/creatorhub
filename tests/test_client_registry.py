"""客户端接入控制面测试: 注册/轮询/取令执行/回执/验证委派(用内存假控制面)。"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.client_registry import (registry_enabled, run, verify_login,
                                 _execute_command, _poll_once, _register,
                                 client_disabled, _pending_receipts)
from app.config import load_config
from app.db import get_session, init_db


class FakeConsoleTransport(httpx.AsyncBaseTransport):
    """内存假控制面: register / poll / verify / risk.set 指令。"""

    def __init__(self):
        self.registers = 0
        self.polls = 0
        self.verifies = 0
        self.commands = []
        self.disabled = False

    async def handle_async_request(self, request):
        method, path = request.method, request.url.path
        if method == "POST" and path == "/api/clients/register":
            self.registers += 1
            return httpx.Response(200, json={
                "ok": True, "client_token": "tok-abc", "poll_interval": 30})
        if method == "POST" and path == "/api/clients/poll":
            self.polls += 1
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={
                "ok": True, "disabled": self.disabled,
                "commands": list(self.commands),
                "poll_interval": 30})
        if method == "POST" and path == "/api/clients/verify":
            self.verifies += 1
            body = json.loads(request.content or b"{}")
            if body.get("password") == "good-pass-1":
                return httpx.Response(200, json={"ok": True,
                                                 "username": body["username"]})
            return httpx.Response(401, json={"ok": False, "detail": "bad"})
        return httpx.Response(404, json={"detail": "no route"})

    async def handle_request(self, request):
        return await self.handle_async_request(request)


def _cfg(console_enabled=True, url="http://console:8100",
         username="c1", password="good-pass-1"):
    from app.config import Config
    cfg = Config()
    cfg.console.enabled = console_enabled
    cfg.console.url = url
    cfg.console.username = username
    cfg.console.password = password
    cfg.console.poll_interval_seconds = 5
    return cfg


class ClientRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import app.models  # noqa: F401  先注册全部模型(状态/审计采集需要表)
        init_db(str(Path(self.tmp.name) / "reg.db"))
        self.fake = FakeConsoleTransport()
        from unittest.mock import patch as _patch
        real = httpx.AsyncClient  # 先捕获真实类, 避免工厂自引用递归
        self._cli_patch = _patch("app.client_registry.httpx.AsyncClient",
                                 _FakeClientFactory(self.fake, real))
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)
        import app.client_registry as cr
        cr._pending_receipts.clear()
        self._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)

    def tearDown(self):
        import app.client_registry as cr
        cr._pending_receipts.clear()
        if self._prev is None:
            os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        else:
            os.environ["CREATORHUB_TEST_AUTH_BYPASS"] = self._prev
        import app.db as dbm
        if dbm._engine is not None:
            try:
                dbm._engine.dispose()
            except Exception:
                pass
            dbm._engine = None

    def test_registry_enabled(self):
        self.assertTrue(registry_enabled(_cfg()))
        self.assertFalse(registry_enabled(_cfg(console_enabled=False)))
        self.assertFalse(registry_enabled(_cfg(username="")))

    def test_register_and_verify(self):
        cfg = _cfg()
        token = asyncio.run(_register(cfg))
        self.assertEqual(token, "tok-abc")
        self.assertEqual(self.fake.registers, 1)
        ok = asyncio.run(verify_login(cfg, "c1", "good-pass-1"))
        self.assertTrue(ok)
        bad = asyncio.run(verify_login(cfg, "c1", "nope"))
        self.assertFalse(bad)
        self.assertEqual(self.fake.verifies, 2)

    def test_poll_executes_risk_command_and_receipt(self):
        import app.client_registry as cr
        cfg = _cfg()
        self.fake.commands = [{"id": 7, "op": "risk.set",
                               "params": {"risk_control": {"enabled": True}}}]
        with patch("app.risk_admin.apply_risk_settings") as apply_mock, \
                patch("app.risk_admin.save_risk_settings"):
            dis = asyncio.run(_poll_once(cfg, "tok-abc"))
        self.assertFalse(dis)
        apply_mock.assert_called_once()
        self.assertEqual(len(cr._pending_receipts), 1)
        self.assertEqual(cr._pending_receipts[0]["command_id"], 7)
        self.assertEqual(cr._pending_receipts[0]["status"], "done")

    def test_run_loop_registers_and_polls_once(self):
        cfg = _cfg()
        with patch("app.client_registry.asyncio.sleep",
                   side_effect=SystemExit("stop")):
            with self.assertRaises(SystemExit):
                asyncio.run(run(cfg))
        self.assertEqual(self.fake.registers, 1)
        self.assertGreaterEqual(self.fake.polls, 1)

    def test_execute_unknown_command_fails(self):
        ok, result = asyncio.run(_execute_command({"op": "nope", "params": {}}))
        self.assertFalse(ok)
        self.assertIn("未知指令", result)

    def test_m2_task_executors(self):
        """远程任务执行器: monitor.run_now 走 engine, 参数缺失给明确错误。"""
        import app.client_registry as cr
        # 参数缺失 → 明确错误(不调 engine)
        ok, result = asyncio.run(_execute_command(
            {"op": "monitor.run_now", "params": {}}))
        self.assertFalse(ok)
        self.assertIn("target_id", result)
        # 引擎未就绪路径(测试环境 engine=None)
        ok, result = asyncio.run(cr._task_monitor_run_now(
            {"target_id": 1}))
        self.assertFalse(ok)
        self.assertIn("引擎未就绪", result)
        # 引擎就绪路径: patch app.main.engine.scan_target
        import app.main as m
        class FakeEngine:
            async def scan_target(self, tid):
                return {"ok": True, "fetched": 3, "tid": tid}
        with patch.object(m, "engine", FakeEngine()):
            ok, result = asyncio.run(cr._task_monitor_run_now(
                {"target_id": 7}))
        self.assertTrue(ok)
        self.assertIn("fetched", result)
        # profile.check 返回结构
        ok, result = asyncio.run(cr._task_profile_check({}))
        self.assertTrue(ok)
        self.assertIn("engine=", result)

    def test_platform_stats_reported(self):
        # 造少量数据再采集: 平台统计出现在状态里
        import app.client_registry as cr
        from app.models import DouyinAccount, MonitorTarget
        with get_session() as s:
            s.add(DouyinAccount(platform="douyin", nickname="a1"))
            s.add(DouyinAccount(platform="xhs", nickname="a2"))
            s.add(MonitorTarget(platform="douyin", sec_uid="t1"))
            s.commit()
        cfg = _cfg()
        status = cr._collect_status(cfg)   # 同步采集
        stats = {p["platform"]: p for p in status["platform_stats"]}
        self.assertIn("douyin", stats)
        self.assertGreaterEqual(stats["douyin"]["accounts"], 1)
        self.assertGreaterEqual(stats["douyin"]["monitors"], 1)
        self.assertIn("xhs", stats)
        self.assertIn("sign_health", status)


class _FakeClientFactory:
    def __init__(self, transport, real_cls):
        self.transport = transport
        self.real_cls = real_cls

    def __call__(self, *a, **kw):
        kw.setdefault("transport", self.transport)
        return self.real_cls(*a, **kw)


if __name__ == "__main__":
    unittest.main()