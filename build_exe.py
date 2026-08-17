"""CreatorHub PRO 打包脚本 (PyInstaller 文件夹版)。

用法:
    python build_exe.py                 # 基础版: 不含离线 ShardX 引擎包
    python build_exe.py --with-engine   # 完整版: 附带离线 ShardX 引擎 (1.6GB, zip 后 ~600MB)

输出:
    dist/CreatorHubPRO/
        CreatorHubPRO.exe    # 主程序 (后端+前端+全依赖)
        node.exe             # 随附 Node (小红书签名)
        shardx-sdk/          # 可选: 离线 ShardX 引擎 (--with-engine)
        config.yaml          # 默认配置
        README.txt           # 分发说明

分发: 整个 dist/CreatorHubPRO 目录打 zip 发给用户, 解压后双击 exe 即用。
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
    """app/ 下的非 py 资源 (前端 / 签名 JS / bin / 缓存)。"""
    web = ROOT / "app" / "web"
    xhs_static = ROOT / "app" / "platforms" / "xhs" / "static"
    bdms = ROOT / "app" / "platforms" / "douyin" / "bdms"
    data = ROOT / "app" / "data"
    out = []
    for src in (web, xhs_static, bdms, data):
        if src.is_dir():
            out.append(f"{src};{src.relative_to(ROOT)}")
    return out


def build(args) -> None:
    py = sys.executable if not VENV_PY.exists() else str(VENV_PY)
    cmd = [
        py, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir",
        "--name", "CreatorHubPRO",        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # 主入口
        str(ROOT / "desktop.py"),
    ]
    # 默认 windowed(隐藏黑窗口/启动日志); --console 时用控制台(调试用)
    if not args.console:
        cmd.insert(5, "--windowed")
    # 收集第三方包 (playwright 驱动 / shardx SDK / 签名库)
    for pkg in ("playwright", "shardx", "curl_cffi", "xhshow", "mini_racer",
                "PyExecJS", "opencv", "cv2", "sqlmodel", "httpx", "imageio_ffmpeg",
                "yt_dlp", "bottle", "webview", "app"):
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
    # node.exe (小红书签名)
    node_src = Path(os.environ.get("NODE_EXE", r"C:\Program Files\nodejs\node.exe"))
    if node_src.is_file():
        shutil.copy2(node_src, DIST / "node.exe")
        print("[build] 已拷贝 node.exe")
    else:
        print("[build] 警告: 未找到 node.exe (小红书创作平台签名将不可用)")
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
                if item.name in ("profiles", ".tmp") or item.name.startswith("."):
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
    (DIST / "README.txt").write_text(
        "CreatorHub PRO\n\n"
        "使用说明:\n"
        "  1. 双击 CreatorHubPRO.exe 启动\n"
        "  2. 需要系统安装 Google Chrome (扫码登录/数据抓取)\n"
        "  3. 首次启动会自动完成 ShardX 引擎检查(随附离线包则免网络)\n"
        "  4. 数据保存在程序目录 data/ 下 (账号/代理/发布记录)\n\n"
        "环境要求:\n"
        "  - Windows 10/11 x64\n"
        "  - Google Chrome\n"
        "  - (可选) 安装.NET WebView2 Runtime (Win11 自带)\n",
        encoding="utf-8")
    print(f"\n[build] 完成 → {DIST}")
    print("[build] 分发: 压缩整个 CreatorHubPRO 目录为 zip")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-engine", action="store_true",
                    help="附带离线 ShardX 引擎包 (引擎资产 ~450MB)")
    ap.add_argument("--console", action="store_true",
                    help="用控制台模式打包(调试用, 默认 windowed 隐藏日志)")
    args = ap.parse_args()
    build(args)
