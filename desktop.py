"""CreatorHub PRO 本地客户端启动器 (pywebview 壳)。

用途:
  - 后台启动 FastAPI (uvicorn), 前台用系统 WebView 打开 http://127.0.0.1:8000
  - 首次运行环境检查: 系统 Chrome / ShardX 引擎(离线包引导)
  - 窗口关闭后干净退出 (杀 uvicorn, 不留后台进程)

开发运行:
    python desktop.py
打包运行 (PyInstaller 文件夹版, 见 build_exe.py):
    dist/CreatorHubPRO/CreatorHubPRO.exe
"""
import os
import shutil
import sys
import threading
from pathlib import Path

import uvicorn

APP_HOST = "127.0.0.1"
APP_PORT = 8000

# 打包后随附文件在 exe 同目录; 开发时在项目根
def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _log(msg: str):
    """windowed 无控制台: 日志写 <bundle>/desktop.log 便于排查。"""
    try:
        with (bundle_root() / "desktop.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _normalize_python_paths():
    """打包后把依赖的 dll/pyd 目录加进 DLL 搜索路径 (PyInstaller one-folder 需要)。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        # patchright 的浏览器驱动 / shardx 引擎依赖 dll
        for sub in ("_internal", "_internal/patchright/driver",
                    "_internal/patchright/driver/package"):
            dll = base / sub
            if dll.is_dir():
                os.environ.setdefault("PATH", str(dll) + os.pathsep + os.environ.get("PATH", ""))


def _ensure_shardx_engine():
    """把随附的离线 ShardX 引擎解压/拷贝到 %LOCALAPPDATA%\\shardx-sdk。

    打包时随附 <bundle>/shardx-sdk/ 完整目录 (engine+widevine+fingerprints+manifest.json),
    首次运行拷贝到 SDK 的 RUNTIME_DIR, 之后 install() 检测 installed=True 即跳过下载
    (无网络也生效)。
    """
    from shardx.runtime import RUNTIME_DIR
    bundled = bundle_root() / "shardx-sdk"
    if not bundled.is_dir():
        return  # 未随附离线包, 交给 SDK 联网下载
    target = Path(RUNTIME_DIR)
    # 已安装且版本一致则跳过 (避免覆盖用户更新后的引擎)
    manifest = target / "manifest.json"
    if target.joinpath("ShardX-Windows").is_dir() and manifest.exists():
        try:
            import json
            bm = json.loads((bundled / "manifest.json").read_text(encoding="utf-8"))
            lm = json.loads(manifest.read_text(encoding="utf-8"))
            if bm.get("installed_chromium_version") == lm.get("installed_chromium_version"):
                return
        except Exception:
            pass
    target.mkdir(parents=True, exist_ok=True)
    # 只拷引擎资产, 排除 profiles(运行时数据)与下载残留
    for item in bundled.iterdir():
        if item.name in ("profiles", ".tmp") or item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.copytree(item, target / item.name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                "*.tmp", ".tmp", "*.download", "*.part"))
        else:
            shutil.copy2(item, target / item.name)
    print(f"[desktop] 已安装离线 ShardX 引擎 → {target}")


def check_env() -> list[str]:
    """环境检查, 返回缺失项列表 (空 = 全部就绪)。"""
    missing = []
    # 1) 系统 Chrome (channel=chrome 依赖)
    try:
        from app.browser.cdp import ChromeLocator
        if ChromeLocator().find() is None:
            missing.append("未检测到系统 Google Chrome (扫码登录/抓取需要)")
    except Exception:
        pass
    # 2) ShardX 引擎
    try:
        from shardx.runtime import RUNTIME_DIR
        from shardx.runtime import Runtime
        if not Runtime().installed:
            missing.append("ShardX 引擎未安装 (首次启动会自动安装/引导离线包)")
    except Exception:
        pass
    return missing


def run_server():
    # 直接 import app.main 的 app 对象传给 uvicorn —— PyInstaller 静态分析能看到
    # app 包并整体收集 (字符串 "app.main:app" 在 frozen 下会找不到)。
    from app.main import app
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="warning")


def main():
    _log("[desktop] main start")
    try:
        _main()
    except Exception as e:
        import traceback
        _log("[desktop] main 异常:\n" + traceback.format_exc())
        raise


def _main():
    _log("[desktop] init")
    _normalize_python_paths()
    _log("[desktop] env paths ok")
    _ensure_shardx_engine()

    missing = check_env()
    if missing:
        msg = "CreatorHub PRO 环境检查:\n\n" + "\n".join(f"  • {m}" for m in missing)
        try:
            import webview
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "环境缺失", 0x10)
        except Exception:
            print("[desktop] 环境缺失:\n" + "\n".join(missing))

    # 后台起服务
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    import webview
    _log("[desktop] create_window...")
    # 显式指定 edgechromium (Windows WebView2) 后端, 避免 windowed 下自动探测异常
    window = webview.create_window(
        "CreatorHub PRO", f"http://{APP_HOST}:{APP_PORT}",
        width=1280, height=860, min_size=(1024, 700))
    _log("[desktop] webview.start...")
    try:
        webview.start(gui="edgechromium")
    except Exception as e:
        import traceback
        _log("[desktop] webview.start 异常:\n" + traceback.format_exc())
        raise
    _log("[desktop] 窗口已关闭")


if __name__ == "__main__":
    main()
