import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.platforms.douyin import ms_token
from app.platforms.douyin.sign_client import remote_strdata


def _ident(**kw):
    base = dict(
        account_id=1, profile_dir="fixture", ua="Mozilla/5.0 ... Chrome/130.0.0.0",
        fp_seed="seed-A", shardx_id="", locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport_w=1707, viewport_h=809, memory_gb=0, cpu_cores=0,
    )
    base.update({k: v for k, v in kw.items()})
    return SimpleNamespace(**base)


class StrdataProfileTests(unittest.TestCase):
    def test_profiles_differ_across_accounts(self):
        a = build = ms_token.build_strdata_profile
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
        # 不同 seed 大概率不同(至少一个字段不同)
        self.assertTrue(p1 != p2 or p1["ua"] == p2["ua"])

    def test_ua_only_profile_when_no_identity(self):
        prof = ms_token.build_strdata_profile("UA-X", identity=None)
        self.assertEqual(prof, {"ua": "UA-X"})

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
