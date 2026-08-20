import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.browser.fingerprint_store import resolve_navigator
from app.platforms.douyin import ms_token
from app.platforms.douyin.sign_client import remote_strdata


def _ident(**kw):
    base = dict(
        account_id=1, profile_dir="fixture", ua="Mozilla/5.0 ... Chrome/130.0.0.0",
        fp_seed="seed-A", shardx_id="", fingerprint_name="", os="",
        locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport_w=1707, viewport_h=809, memory_gb=0, cpu_cores=0,
    )
    base.update({k: v for k, v in kw.items()})
    return SimpleNamespace(**base)


class StrdataProfileTests(unittest.TestCase):
    def setUp(self):
        # 默认机器可能有 E:/fingerprint-db → 屏蔽文件解析, 使派生测试与机器无关
        patcher = patch.object(ms_token, "_default_db_dir", return_value="")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_profiles_differ_across_accounts(self):
        build = ms_token.build_strdata_profile
        pa = build("UA-A", identity=_ident(fp_seed="a", ua="UA-A"))
        pb = build("UA-B", identity=_ident(fp_seed="b", ua="UA-B"))
        self.assertNotEqual(pa, pb)

    def test_profile_respects_explicit_hardware_overrides(self):
        prof = ms_token.build_strdata_profile(
            "UA", identity=_ident(memory_gb=16, cpu_cores=24))
        self.assertEqual(prof["deviceMemory"], 16)
        self.assertEqual(prof["hardwareConcurrency"], 24)

    def test_profile_derives_hardware_deterministically_from_seed(self):
        p1 = ms_token.build_strdata_profile(
            "UA", identity=_ident(fp_seed="seed-A"))
        p1b = ms_token.build_strdata_profile(
            "UA", identity=_ident(fp_seed="seed-A"))
        p2 = ms_token.build_strdata_profile(
            "UA", identity=_ident(fp_seed="seed-B"))
        self.assertEqual(p1, p1b)                 # 同账号稳定
        self.assertIn(p1["hardwareConcurrency"], (4, 8, 12, 16, 24))
        self.assertIn(p1["deviceMemory"], (4, 6, 8, 12, 16, 32))

    def test_ua_only_profile_when_no_identity(self):
        prof = ms_token.build_strdata_profile("UA-X", identity=None)
        self.assertEqual(prof, {"ua": "UA-X"})

    def test_build_profile_uses_real_navigator_when_provided(self):
        prof = ms_token.build_strdata_profile(
            "UA", identity=_ident(),
            navigator={"device_memory": 32, "hardware_concurrency": 16,
                       "platform_value": "Win32", "language": "en-US"})
        self.assertEqual(prof["deviceMemory"], 32)
        self.assertEqual(prof["hardwareConcurrency"], 16)
        self.assertEqual(prof["platform"], "Win32")
        self.assertEqual(prof["language"], "en-US")

    def test_strdata_cache_keys_isolated_per_account(self):
        k1 = ms_token._strdata_cache_key("UA-A", _ident(fp_seed="a", ua="UA-A"))
        k2 = ms_token._strdata_cache_key("UA-B", _ident(fp_seed="b", ua="UA-B"))
        self.assertNotEqual(k1, k2)
        self.assertTrue(k1)
        self.assertEqual(ms_token._strdata_cache_key("", None), "")

    def test_strdata_cache_read_back_correct_key(self):
        ms_token._save_strdata("STRDATA-A", "key-a")
        ms_token._save_strdata("STRDATA-B", "key-b")
        try:
            self.assertEqual(ms_token._load_strdata("key-a"), "STRDATA-A")
            self.assertEqual(ms_token._load_strdata("key-b"), "STRDATA-B")
        finally:
            ms_token._save_strdata("", "key-a")
            ms_token._save_strdata("", "key-b")


class FpDbNavigatorTests(unittest.TestCase):
    def test_resolve_navigator_from_local_fingerprint_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "database"
            db.mkdir()
            for name, cores, mem in (("p-a", 8, 16), ("p-b", 12, 32)):
                (db / f"{name}.json").write_text(
                    json.dumps({"navigator": {
                        "platform": "Windows", "platform_value": "Win32",
                        "hardware_concurrency": cores, "device_memory": mem,
                        "language": "zh-CN",
                    }}), encoding="utf-8")
            nav = resolve_navigator(tmp, fp_seed="abc", platform="win")
            self.assertIsNotNone(nav)
            self.assertIn(nav.get("hardware_concurrency"), (8, 12))
            self.assertIn(nav.get("device_memory"), (16, 32))
            # 确定性选择: 同一 seed 结果一致
            nav2 = resolve_navigator(tmp, fp_seed="abc", platform="win")
            self.assertEqual(nav, nav2)

    def test_resolve_navigator_none_on_missing_db_or_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(resolve_navigator(tmp, fp_seed="x"))
            self.assertIsNone(resolve_navigator(tmp, fingerprint_name="nope"))


class RemoteStrdataParamTests(unittest.TestCase):
    def test_remote_strdata_forwards_profile(self):
        with patch("app.platforms.douyin.sign_client._post",
                   return_value={"ok": True, "strData": "FP"}) as post:
            out = remote_strdata({"ua": "U", "deviceMemory": 16})
        self.assertEqual(out, "FP")
        post.assert_called_once_with(
            "strdata", {"ua": "U", "deviceMemory": 16})

    def test_remote_strdata_empty_payload_when_no_profile(self):
        with patch("app.platforms.douyin.sign_client._post",
                   return_value={"ok": True, "strData": "FP"}) as post:
            remote_strdata()
        post.assert_called_once_with("strdata", {})

    def test_remote_strdata_none_on_failure(self):
        with patch("app.platforms.douyin.sign_client._post",
                   side_effect=RuntimeError("down")):
            self.assertIsNone(remote_strdata({"ua": "U"}))


if __name__ == "__main__":
    unittest.main()
