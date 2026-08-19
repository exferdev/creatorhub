import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import app.db as db
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (AccountRiskState, DouyinAccount, KeywordCollectionJob)
from app.risk import OperationKind


class _Identity:
    timezone_id = "Asia/Shanghai"


class _BrowserStub:
    def __init__(self):
        self.locks = {}

    def lock_for(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _Identity()


class CollectionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    """覆盖 MonitorEngine 关键词采集任务的状态机：阻塞元数据 / 取消 / 恢复 / 调度。"""

    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "sched.db"))
        self.cfg = Config()
        self.browser = _BrowserStub()
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin", nickname="fixture", status="active",
                douyin_id="acc-id",
                storage_state='{"cookies":[{"name":"sessionid","value":"x"}]}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id
        self.engine = MonitorEngine(self.cfg, self.browser)
        self.engine.keyword_collector = AsyncMock()
        self.engine.keyword_collector.run = AsyncMock(return_value={
            "canceled": False, "errors": 0, "contents": 2, "comments": 3,
        })

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _job(self, **overrides):
        values = dict(platform="douyin", account_id=self.account_id,
                      keywords='["露营"]', status="pending")
        values.update(overrides)
        with db.get_session() as session:
            job = KeywordCollectionJob(**values)
            session.add(job)
            session.commit()
            session.refresh(job)
            return job.id

    async def test_deferred_by_risk_cooldown_records_blocked_metadata(self):
        job_id = self._job()
        with db.get_session() as session:
            session.add(AccountRiskState(
                account_id=self.account_id,
                risk_level=1,
                cooldown_until=datetime.utcnow() + timedelta(minutes=30),
            ))
            session.commit()

        result = await self.engine.run_collection_job(job_id)

        self.assertTrue(result["deferred"])
        self.assertEqual(result["signal"], "cooldown")
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            self.assertEqual(job.status, "pending")
            self.assertEqual(job.blocked_signal, "cooldown")
            self.assertEqual(job.blocked_operation,
                             OperationKind.READ_HEAVY.value)
            self.assertIn("冷却", job.blocked_reason)
            self.assertIsNotNone(job.blocked_at)
            self.assertIsNotNone(job.next_allowed_at)
        self.engine.keyword_collector.run.assert_not_awaited()

    async def test_cancel_requested_marks_job_canceled(self):
        job_id = self._job(status="running", cancel_requested=True)

        result = await self.engine.run_collection_job(job_id)

        self.assertTrue(result["canceled"])
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            self.assertEqual(job.status, "canceled")
            self.assertIsNotNone(job.finished_at)
        self.engine.keyword_collector.run.assert_not_awaited()

    async def test_missing_account_fails_job_without_browser(self):
        job_id = self._job(account_id=999_999)

        result = await self.engine.run_collection_job(job_id)

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            self.assertEqual(job.status, "failed")
            self.assertIn("不存在", job.error)
        self.engine.keyword_collector.run.assert_not_awaited()

    async def test_success_path_runs_collector_and_marks_done(self):
        job_id = self._job()

        result = await self.engine.run_collection_job(job_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["contents"], 2)
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            self.assertEqual(job.status, "done")
            self.assertEqual(job.blocked_reason, "")
        self.engine.keyword_collector.run.assert_awaited_once()

    async def test_wake_deferred_tasks_clears_blocks(self):
        job_id = self._job(
            status="pending",
            blocked_reason="旧的冷却阻塞",
            blocked_signal="cooldown",
            blocked_operation=OperationKind.READ_HEAVY.value,
        )

        woken = self.engine._wake_deferred_tasks(self.account_id)

        self.assertGreaterEqual(woken, 1)
        with db.get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            self.assertEqual(job.blocked_reason, "")
            self.assertEqual(job.blocked_signal, "")
            self.assertIsNone(job.blocked_at)
            self.assertIsNone(job.next_allowed_at)

    async def test_process_collection_jobs_dispatches_pending(self):
        job_id = self._job()
        self.engine.enqueue_collection_job = MagicMock(return_value=True)

        await self.engine._process_collection_jobs()

        self.engine.enqueue_collection_job.assert_called_once_with(job_id)


if __name__ == "__main__":
    unittest.main()
