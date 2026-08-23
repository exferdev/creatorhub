"""CreatorHub PRO 打包脚本 (PyInstaller 文件夹版)。

用法:
    python build_exe.py                 # 基础版: 不含离线 ShardX 引擎包
    python build_exe.py --with-engine   # 完整版: 附带离线 ShardX 引擎 (1.6GB, zip 后 ~600MB)

平台: PyInstaller 不支持交叉编译 —— Windows 版需在 Windows 上构建 (输出 .exe),
macOS 版需在 macOS 上构建 (输出 .app/.dmg)。引擎离线包按平台匹配
ShardX-Windows / ShardX-macOS (desktop.py 启动时自动探测)。

输出:
    dist/CreatorHubPRO/
        CreatorHubPRO.exe    # 主程序 (后端+前端+全依赖)
        shardx-sdk/          # 可选: 离线 ShardX 引擎 (--with-engine, 随平台)
        config.yaml          # 默认配置 (server.port 决定客户端服务端口)
        README.txt           # 分发说明

分发: 整个 dist/CreatorHubPRO 目录打 zip 发给用户, 解压后双击 exe 即用
(macOS 分发 .app 打包的 DMG)。
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "CreatorHubPRO"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _collect_app_data() -> list[str]:
    """app/ 下的非 py 资源 (前端 / bin / 缓存 / IM 模板)。"""
    web = ROOT / "app" / "web"
    xhs_static = ROOT / "app" / "platforms" / "xhs" / "static"
    data = ROOT / "app" / "data"
    im_template = ROOT / "app" / "platforms" / "douyin" / "send_template.bin"
    out = []
    for src in (web, xhs_static, data):
        if src.is_dir():
            out.append(f"{src};{src.relative_to(ROOT)}")
    if im_template.is_file():
        out.append(f"{im_template};{im_template.relative_to(ROOT).parent}")
    return out


def build(args) -> None:
    # 每次构建先清空 dist, 避免上次模式的残留 (如 onedir 的 lib/_internal) 混入
    import shutil as _sh
    _sh.rmtree(ROOT / "dist", ignore_errors=True)
    py = sys.executable if not VENV_PY.exists() else str(VENV_PY)
    DIST = ROOT / "dist" if args.onefile else ROOT / "dist" / "CreatorHubPRO"
    cmd = [
        py, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--name", "CreatorHubPRO",        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if not args.onefile:
        # 依赖目录不叫 _internal 而叫 lib (更中性, 且会设隐藏属性, 资源管理器默认不显示)
        cmd += ["--contents-directory", "lib"]
    # 默认 windowed(隐藏黑窗口/启动日志); --console 时用控制台(调试用)
    if not args.console:
        cmd.append("--windowed")
    # 主入口
    cmd.append(str(ROOT / "desktop.py"))
    # 收集第三方包 (patchright 驱动 / shardx SDK / 签名库 / 后台鉴权)
    for pkg in ("patchright", "shardx", "curl_cffi", "xhshow",
                "opencv", "cv2", "sqlmodel", "httpx", "imageio_ffmpeg",
                "yt_dlp", "bottle", "webview", "app",
                "fastapi_users", "pwdlib", "argon2", "slowapi", "limits"):
        cmd.append(f"--collect-all={pkg}")
    # app 数据资源
    for d in _collect_app_data():
        cmd.append(f"--add-data={d}")
    # 隐藏导入 (动态 import)
    for h in ("uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocol.http.auto",
              "uvicorn.protocol.websockets.auto", "uvicorn.lifespan.on",
              "sqlmodel.main", "websockets"):
        cmd.append(f"--hidden-import={h}")
    cmd.append("--exclude-module=tkinter")
    cmd.append("--exclude-module=matplotlib")

    print("[build] PyInstaller:", " ".join(cmd[:6]), "...")
    import subprocess
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print("[build] PyInstaller 失败")
        sys.exit(r.returncode)

    # 拷贝随附文件到 dist
    DIST.mkdir(parents=True, exist_ok=True)

    if not args.onefile:
        # 隐藏依赖目录 (lib = 原 _internal; 资源管理器默认不可见, 减少对用户的干扰)
        import ctypes
        for name in ("lib", "_internal"):
            dep_dir = DIST / name
            if dep_dir.is_dir():
                FILE_ATTRIBUTE_HIDDEN = 0x2
                FILE_ATTRIBUTE_SYSTEM = 0x4
                try:
                    ctypes.windll.kernel32.SetFileAttributesW(
                        str(dep_dir),
                        FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
                    print(f"[build] 已隐藏依赖目录: {dep_dir}")
                except Exception as e:
                    print(f"[build] 设置隐藏属性失败: {e!r}")
                break
    # config.yaml
    if (ROOT / "config.yaml").exists():
        shutil.copy2(ROOT / "config.yaml", DIST / "config.yaml")
    # 可选: 离线 ShardX 引擎
    if args.with_engine:
        sdk_src = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "shardx-sdk"
        if sdk_src.is_dir():
            print("[build] 拷贝离线 ShardX 引擎 (引擎资产, 排除运行时 profiles/.tmp)...")
            # 只拷引擎资产 (ShardX-Windows / Widevine / fingerprints / manifest.json),
            # 排除 profiles(运行中的浏览器 user-data-dir, 含锁文件) 和下载残留。
            dst = DIST / "shardx-sdk"
            dst.mkdir(parents=True, exist_ok=True)
            for item in sdk_src.iterdir():
                # 排除 profiles(运行时数据) / fingerprints(云端取, 不随包附带) / 下载残留
                if item.name in ("profiles", "fingerprints", ".tmp") or item.name.startswith("."):
                    continue
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(
                                        "*.tmp", ".tmp", "*.download", "*.part"))
                else:
                    shutil.copy2(item, dst / item.name)
            print("[build] 已拷贝离线引擎 (排除 profiles 运行时数据)")
        else:
            print(f"[build] 警告: 未找到 {sdk_src}, 跳过离线引擎")
    # README
    single = "是" if args.onefile else "否"
    (DIST / "README.txt").write_text(
        "CreatorHub PRO\n\n"
        "使用说明:\n"
        "  1. 双击 CreatorHubPRO.exe 启动 (macOS: 打开 CreatorHubPRO.app)\n"
        f"  单文件模式: {single}。非单文件模式下的 lib 目录是程序运行依赖, 请勿删除。\n"
        "  2. 需要系统安装 Google Chrome (扫码登录/数据抓取)\n"
        "  3. 首次启动会自动完成 ShardX 引擎检查(随附离线包则免网络)\n"
        "  4. 数据保存在程序目录 data/ 下 (账号/代理/发布记录)\n\n"
        "排障:\n"
        "  服务端口以随附 config.yaml 的 server.port 为准, 启动后自动打开对应地址。\n"
        "  日志文件: Windows: %LOCALAPPDATA%\\CreatorHubPRO\\desktop.log\n"
        "            macOS: ~/Library/Logs/CreatorHubPRO/desktop.log\n"
        "  打不开页面时先看日志, 常见原因: 端口被占用 / 缺 WebView2(Runtime) /\n"
        "  缺少系统 Google Chrome / 杀毒拦截。设置环境变量 DEBUG=1 可保留控制台并输出全量日志。\n\n"
        "环境要求:\n"
        "  - Windows 10/11 x64 或 macOS 12+ (Apple Silicon/Intel)\n"
        "  - Google Chrome\n"
        "  - (可选) 安装.NET WebView2 Runtime (Win11 自带)\n",
        encoding="utf-8")
    print(f"\n[build] 完成 → {DIST}")
    print("[build] 分发: 压缩整个 CreatorHubPRO 目录为 zip")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-engine", action="store_true",
                    help="附带离线 ShardX 引擎包 (引擎资产 ~450MB)")
    ap.add_argument("--onefile", action="store_true",
                    help="单文件模式: 依赖全部打进 exe (启动时解压到临时目录, 略慢; 便于分发)")
    ap.add_argument("--console", action="store_true",
                    help="用控制台模式打包(调试用, 默认 windowed 隐藏日志)")
    args = ap.parse_args()
    build(args)
