"""M5 设置/告警测试: 触发逻辑/去重/设置保存/通道/页面。"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

starlette_admin = pytest.importorskip("starlette_admin", reason="Console 独立环境")

from fastapi.testclient import TestClient
from sqlmodel import select

import console.db as cdb
from console.apprise_alerts import (check_client_offline, check_sign_health,
                                    check_task_failures, get_setting, set_setting)
from console.console_auth import hash_password
from console.models import (AppriseChannel, ClientAccount, ClientCommand,
                            ClientAudit, ConsoleAudit, ConsoleUser, Setting)


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._prev = os.environ.get("CREATORHUB_TEST_AUTH_BYPASS")
        os.environ.pop("CREATORHUB_TEST_AUTH_BYPASS", None)
        cls._prev_db = os.environ.get("CONSOLE_DB_PATH")
        os.environ["CONSOLE_DB_PATH"] = str(Path(cls.tmp) / "console.db")
        cdb.init_db(os.environ["CONSOLE_DB_PATH"])
        with cdb.get_session() as s:
            s.add(ConsoleUser(username="boss", email="boss@c", role="admin",
                              is_superuser=True,
                              hashed_password=hash_password("boss-pass-1")))
            s.commit()
        # 每类 reload 获得全新 app(跨类隔离)
        import importlib, console.main as _cm
        cls.cm = importlib.reload(_cm)
        cls.app = cls.cm.app

    @classmethod
    def tearDownClass(cls):
        for var, prev in (("CREATORHUB_TEST_AUTH_BYPASS", cls._prev),
                          ("CONSOLE_DB_PATH", cls._prev_db)):
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

    def _seed(self):
        from console.apprise_alerts import (set_setting as _ss,
                                            _ALARMED_COMMAND_IDS, _ALARM_STATE)
        # 清模块级去重状态(跨测试累积会吞掉本轮告警)
        _ALARMED_COMMAND_IDS.clear()
        _ALARM_STATE["offline"].clear()
        _ALARM_STATE["sign"].clear()
        # 强制默认告警开关(页面测试可能改过 Setting)
        _ss("offline_alert_enabled", "1")
        _ss("task_fail_alert_enabled", "1")
        _ss("sign_health_alert_enabled", "1")
        with cdb.get_session() as s:
            # 幂等: 清掉同名账号/指令(类共享库, 各测试独立重建)
            for a in s.exec(select(ClientAccount)).all():
                s.delete(a)
            for c in s.exec(select(ClientCommand)).all():
                s.delete(c)
            s.commit()
            s.add(AppriseChannel(name="web", notify_urls="json://x.test/webhook",
                                 enabled=True))
            s.add(ClientAccount(username="c-off", password_hash="ph",
                                client_token="t1",
                                last_seen_at=datetime.utcnow() - timedelta(
                                    days=1)))
            s.add(ClientAccount(username="c-ok", password_hash="ph",
                                client_token="t2",
                                last_seen_at=datetime.utcnow()))
            s.add(ClientCommand(client_id=1, client_name="c-off", op="publish.run",
                                params="{}", status="failed", result="出错了",
                                created_at=datetime.utcnow()))
            s.commit()

    def test_offline_alert_trigger_and_dedup(self):
        self._seed()
        with patch("console.apprise_alerts.send_alert", return_value=True) as sa:
            n1 = self._run(check_client_offline(datetime.utcnow()))
            self.assertEqual(n1, 1)          # c-off 告警一次
            n2 = self._run(check_client_offline(datetime.utcnow()))
            self.assertEqual(n2, 0)          # 去重: 不再发
        # 恢复后重置: 更新 last_seen 后不再告警且状态复位
        with cdb.get_session() as s:
            a = s.exec(select(ClientAccount).where(
                ClientAccount.username == "c-off")).first()
            a.last_seen_at = datetime.utcnow()
            s.add(a)
            s.commit()
        with patch("console.apprise_alerts.send_alert", return_value=True) as sa:
            n3 = self._run(check_client_offline(datetime.utcnow()))
            self.assertEqual(n3, 0)

    def test_task_failure_alert_dedup(self):
        self._seed()
        with patch("console.apprise_alerts.send_alert", return_value=True) as sa:
            n1 = self._run(check_task_failures(datetime.utcnow()))
            self.assertEqual(n1, 1)
            n2 = self._run(check_task_failures(datetime.utcnow()))
            self.assertEqual(n2, 0)          # 指令 id 去重

    def test_sign_health_threshold_alert(self):
        self._seed()
        with cdb.get_session() as s:
            a = s.exec(select(ClientAccount).where(
                ClientAccount.username == "c-ok")).first()
            a.status_json = json.dumps({"sign_health": {
                "douyin": {"total": 10, "ok_rate": 0.3, "errors": 7,
                           "p95_ms": 5, "last_error": ""}}})
            s.add(a)
            s.commit()
        with patch("console.apprise_alerts.send_alert", return_value=True) as sa:
            n1 = self._run(check_sign_health(datetime.utcnow()))
            self.assertEqual(n1, 1)
            n2 = self._run(check_sign_health(datetime.utcnow()))
            self.assertEqual(n2, 0)          # 30 分钟去重
        # 清状态表以便其他用例
        from console.apprise_alerts import _ALARM_STATE
        _ALARM_STATE["sign"].clear()

    @staticmethod
    def _run(coro):
        import asyncio as _a
        loop = _a.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_settings_kv_and_retention(self):
        set_setting("retention_days", "1")
        self.assertEqual(get_setting("retention_days"), "1")
        # 造过期审计 → 清理
        with cdb.get_session() as s:
            s.add(ClientAudit(client_id=1, client_name="x", action="a",
                              created_at=datetime.utcnow() - timedelta(days=5)))
            s.commit()
        import asyncio as _a
        from console.apprise_alerts import run_retention_once
        _a.run(run_retention_once())
        with cdb.get_session() as s:
            rows = s.exec(select(ClientAudit)).all()
        self.assertEqual(len(rows), 0)

    def test_settings_page_and_forms(self):
        with TestClient(self.app) as client:
            client.post("/admin/login", data={"username": "boss",
                                              "password": "boss-pass-1"})
            r = client.get("/admin/settings")
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIn("告警通道", r.text)
            self.assertIn("保留天数", r.text)
            # 保存设置
            r = client.post("/admin/settings", data={
                "action": "settings_save",
                "offline_alert_enabled": "1",
                "offline_after_seconds": "240",
                "task_fail_alert_enabled": "0",
                "sign_health_alert_enabled": "1",
                "sign_ok_rate_threshold": "0.9",
                "retention_days": "60"})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIn("设置已保存", r.text)
            self.assertEqual(get_setting("offline_after_seconds"), "240")
            self.assertEqual(get_setting("task_fail_alert_enabled"), "0")
            self.assertEqual(get_setting("retention_days"), "60")
            # 添加通道 + 测试(打 json:// 无实际外发, 直接断言表单流)
            r = client.post("/admin/settings", data={
                "action": "channel_add", "ch_name": "d1",
                "ch_urls": "json://x.test/hook"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("已添加", r.text)
            with cdb.get_session() as s:
                chs = s.exec(select(AppriseChannel)).all()
            self.assertEqual(len(chs), 2)
            self.assertIn("通道", client.get("/admin/settings").text)


if __name__ == "__main__":
    unittest.main()