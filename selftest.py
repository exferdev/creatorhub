"""自检脚本(离线):验证基础能力 + 关键依赖是否就位。
  python selftest.py
注:抓取/登录现在走真实浏览器(Patchright),签名已完全云端(js-sign-service),
   所以这里只做基础自检 + 远程签名客户端导入检查 + Patchright 可用性检查。
"""
import sys


def check_sign_client() -> bool:
    """签名已完全云端: 离线校验远程签名客户端模块可导入即可(不打网络)。"""
    try:
        from app.platforms.douyin.sign_client import (
            remote_abogus, remote_xbogus, remote_strdata, remote_mstoken)
        from app.platforms.xhs.creator_sign import (
            generate_xsc, generate_x_rap_param, available)
        assert callable(remote_abogus) and callable(generate_xsc)
        print("[签名服务] OK: 抖音/小红书远程签名客户端可导入(完全云端)")
        return True
    except Exception as e:
        print(f"[签名服务] FAIL: {e}")
        return False


def check_risk_control() -> bool:
    try:
        from app.config import Config
        from app.risk import RiskController, network_key

        cfg = Config()
        controller = RiskController(cfg)
        assert controller.policy.enabled
        assert controller.policy.cooldown_steps_seconds == [1800, 7200, 21600, 86400]
        assert network_key("") == "direct"
        proxy_key = network_key("http://user:secret@proxy.example:8000")
        assert proxy_key.startswith("proxy:") and "secret" not in proxy_key
        print("[平台风控] OK: 持久化策略、冷却阶梯和网络出口分组可用")
        return True
    except Exception as e:
        print(f"[平台风控] FAIL: {e}")
        return False


def check_patchright() -> bool:
    try:
        import patchright  # noqa: F401
        from patchright.sync_api import sync_playwright
    except Exception as e:
        print(f"[Patchright] FAIL: 未安装: {e}")
        print("   运行: python creatorhub.py install")
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        print("[Patchright] OK: Chromium 可启动")
        return True
    except Exception as e:
        print(f"[Patchright] FAIL: 启动失败: {e}")
        print("   运行: python creatorhub.py install")
        return False


def check_share_downloader() -> bool:
    try:
        import yt_dlp
        from app.engine.share_downloader import extract_share_urls

        links = extract_share_urls(
            "中文分享：https%3A%2F%2Fv.douyin.com%2Fexample%2F 😄"
        )
        assert links and links[0].host == "v.douyin.com"
        print(f"[链接下载] OK: yt-dlp {yt_dlp.version.__version__}，分享文案解析正常")
        return True
    except Exception as e:
        print(f"[链接下载] FAIL: {e}")
        print("   运行: python -m pip install -r requirements.txt")
        return False


if __name__ == "__main__":
    checks = (
        check_sign_client(),
        check_risk_control(),
        check_patchright(),
        check_share_downloader(),
    )
    if all(checks):
        print("\n自检通过。启动:  python creatorhub.py")
        raise SystemExit(0)
    print("\n自检未通过，请先运行:  python creatorhub.py install")
    raise SystemExit(1)
