import json
import unittest
from unittest.mock import patch

from app.platforms.xhs import creator_sign as cs

A1 = "a1_fixture_0123456789"

_ALL_RESP = {
    "ok": True,
    "x_s": "XYS_2UQhREMOTE",
    "x_t": 1787123456789,
    "x_s_common": "2UQAPsREMOTE",
    "x_b3_traceid": "b" * 16,
    "x_xray_traceid": "x" * 32,
}


class XhsRemoteSignTests(unittest.TestCase):
    def setUp(self):
        cs._AVAILABLE = None
        cs._AVAILABLE_AT = 0.0

    def tearDown(self):
        cs._AVAILABLE = None
        cs._AVAILABLE_AT = 0.0

    def test_generate_xsc_uses_all_endpoint(self):
        with patch.object(cs, "_post", return_value=_ALL_RESP) as post:
            out = cs.generate_xsc(A1, "/api/sns/web/v1/user/me?tab=0")

        self.assertEqual(sorted(out), [
            "x-b3-traceid", "x-s", "x-s-common", "x-t", "x-xray-traceid"])
        self.assertEqual(out["x-s"], _ALL_RESP["x_s"])
        self.assertIsInstance(out["x-t"], str)
        post.assert_called_once_with(
            "all", {"a1": A1, "api": "/api/sns/web/v1/user/me?tab=0",
                    "method": "GET", "data": ""})

    def test_generate_xsc_main_forwards_method(self):
        with patch.object(cs, "_post", return_value=_ALL_RESP) as post:
            cs.generate_xsc_main(A1, "/api/sns/web/v1/comment/post", '{"x":1}', "POST")
        _, payload = post.call_args[0]
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["data"], '{"x":1}')

    def test_generate_xs_xs_common_unpacks(self):
        with patch.object(cs, "_post", return_value=_ALL_RESP):
            xs, xt, xsc = cs.generate_xs_xs_common(
                A1, "/api/sns/web/v2/user/posted?num=12")
        self.assertEqual(xs, _ALL_RESP["x_s"])
        self.assertEqual(xt, _ALL_RESP["x_t"])
        self.assertEqual(xsc, _ALL_RESP["x_s_common"])

    def test_generate_x_rap_param_serializes_dict_data(self):
        with patch.object(cs, "_post", return_value={"ok": True,
                                                     "x_rap_param": "ByQB"}) as post:
            out = cs.generate_x_rap_param("/api/sns/web/v1/foo", {"a": 1})
        self.assertEqual(out, "ByQB")
        _, payload = post.call_args[0]
        self.assertEqual(payload["api"], "/api/sns/web/v1/foo")
        self.assertEqual(json.loads(payload["data"]), {"a": 1})

    def test_cos_signature_calls_cos_endpoint(self):
        with patch.object(cs, "_post",
                          return_value={"ok": True, "signature": "abc123",
                                        "authorization": "auth"}) as post:
            out = cs.cos_signature("message", "file_id_x", 123)
        self.assertEqual(out, "abc123")
        _, payload = post.call_args[0]
        self.assertEqual(payload["message"], "message")
        self.assertEqual(payload["file_id"], "file_id_x")
        self.assertEqual(payload["content_length"], 123)
        self.assertEqual(payload["host"], "ros-upload.xiaohongshu.com")

    def test_traceids_call_traceid_endpoint(self):
        resp = {"ok": True, "x_b3_traceid": "b" * 16, "x_xray_traceid": "x" * 32}
        with patch.object(cs, "_post", return_value=resp):
            self.assertEqual(cs.gen_b3_traceid(), "b" * 16)
            self.assertEqual(cs.gen_xray_traceid(), "x" * 32)

    def test_payload_never_contains_cookie_credentials(self):
        captured = {}

        def fake_post(algorithm, payload):
            captured[algorithm] = payload
            if algorithm == "x_rap":
                return {"ok": True, "x_rap_param": "ByQB"}
            return _ALL_RESP

        with patch.object(cs, "_post", side_effect=fake_post):
            cs.generate_xsc(A1, "/api/sns/web/v1/user/me", "")
            cs.generate_x_rap_param("/api/sns/web/v1/foo", "")

        blob = json.dumps(captured, ensure_ascii=False).lower()
        for secret in ("sessionid", "web_session", "customerclientid",
                       "access-token", "galaxy"):   # 完整凭据绝不外发
            self.assertNotIn(secret, blob)

    def test_post_raises_on_non_ok_response(self):
        import curl_cffi.requests as cr
        from types import SimpleNamespace

        resp = SimpleNamespace(
            json=lambda: {"ok": False, "error": "a1 不能为空"},
            status_code=400)
        with patch.object(cr, "post", return_value=resp):
            with self.assertRaises(RuntimeError):
                cs._post("xs", {"a1": "", "api": "/api/test"})

    def test_sign_failure_propagates_runtime_error(self):
        with patch.object(cs, "_post",
                          side_effect=RuntimeError("sign service error")):
            with self.assertRaises(RuntimeError):
                cs.generate_xsc("", "/api/test")

    def test_available_true_when_reachable_and_caches(self):
        with patch.object(cs, "_post", return_value={"ok": True}) as post:
            self.assertTrue(cs.available())
            self.assertTrue(cs.available())      # 命中缓存,不再打网络
            self.assertEqual(post.call_count, 1)

    def test_available_false_on_failure_then_recovers(self):
        with patch.object(cs, "_post", side_effect=RuntimeError("down")):
            self.assertFalse(cs.available())
        cs._AVAILABLE = None
        cs._AVAILABLE_AT = 0.0                    # 清缓存,下次探测
        with patch.object(cs, "_post", return_value={"ok": True}):
            self.assertTrue(cs.available())


if __name__ == "__main__":
    unittest.main()
