"""CreatorHub PRO 本地客户端启动器 (pywebview 壳, 支持 Windows/macOS/Linux)。

用途:
  - 后台启动 FastAPI (uvicorn), 前台用系统 WebView 打开 http://127.0.0.1:<port>
  - 端口/绑定地址从随附的 config.yaml 读取 (server.port / server.host),
    不再硬编码 8000 —— 修掉"异机启动后按配置端口访问失败"的端口错位问题。
  - 日志写入可写目录: %LOCALAPPDATA%/CreatorHubPRO/desktop.log (Windows)
    或 ~/Library/Logs/CreatorHubPRO/desktop.log (macOS) —— 安装到系统目录
    (如 Program Files) 时不再因权限丢失日志。
  - 启动后轮询 /health 直到就绪; 后端启动失败或超时 -> 弹窗给出错误信息、
    日志路径与排障提示 (端口占用/WebView2 缺失/缺依赖)。
  - 设置 DEBUG=1 (或 --debug) 时保留控制台并开 uvicorn 全量日志, 方便远端排障。

开发运行:
    python desktop.py             # Windows: WebView2 (edgechromium)
    python desktop.py --debug     # 带控制台 + 全量日志
打包运行 (PyInstaller 文件夹版, 见 build_exe.py):
    dist/CreatorHubPRO/CreatorHubPRO.exe   (Windows)
    dist/CreatorHubPRO.dmg / .app          (macOS, 在 Mac 上构建)
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import uvicorn

APP_HOST = "127.0.0.1"
APP_PORT = 8000  # 兜底默认; 实际以 config.yaml 的 server.port 为准

# 全局保存后端线程异常, 供主线程在启动超时/失败时给出明确报错
_SERVER_ERROR: BaseException | None = None


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


# 打包后随附文件在 exe 同目录; 开发时在项目根
def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def log_dir() -> Path:
    """日志目录: 用户可写目录优先, 保证装在任何位置都能写。"""
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CreatorHubPRO"
    elif is_macos():
        base = Path.home() / "Library" / "Logs" / "CreatorHubPRO"
    else:
        base = bundle_root() / "logs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        return bundle_root()  # 兜底 (开发态/可写 bundle)


_log_file = None
_log_lock = threading.Lock()
_STDOUT_REDIRECTED = False


def _log(msg: str):
    """带时间戳写日志文件 + 尽力打屏 (windowed 模式无控制台则只写文件;
    stdout 已被重定向到日志文件时不再重复 print, 避免日志每行出现两次)。"""
    global _log_file
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _log_lock:
        if _log_file is None:
            _log_file = (log_dir() / "desktop.log").open("a", encoding="utf-8")
        try:
            _log_file.write(f"[{ts}] {msg}\n")
            _log_file.flush()
        except Exception:
            pass
    if not _STDOUT_REDIRECTED:
        try:
            print(f"[{ts}] {msg}", flush=True)
        except Exception:
            pass


def read_server_bind() -> tuple[str, int]:
    """从 config.yaml 读取 server.host / server.port (轻量解析, 不依赖 PyYAML)。"""
    host, port = APP_HOST, APP_PORT
    cfg_file = bundle_root() / "config.yaml"
    if not cfg_file.exists():
        return host, port
    try:
        text = cfg_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return host, port
    in_server = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "\t")):
            in_server = line.strip() == "server:"
            continue
        if not in_server or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        value = value.strip("'\"")
        if key == "host" and value:
            host = value
        elif key == "port":
            try:
                port = int(value)
            except ValueError:
                pass
    return host, port


def _normalize_python_paths():
    """打包后把依赖的 dll/pyd 目录加进搜索路径 (仅 Windows, PyInstaller one-folder 需要)。

    依赖目录名兼容新打包 (lib, 原 _internal 改名) 与旧打包 (_internal)。"""
    if not (is_windows() and getattr(sys, "frozen", False)):
        return
    base = Path(sys.executable).parent
    # patchright 的浏览器驱动 / shardx 引擎依赖 dll
    candidates = [
        base / "lib", base / "_internal",
        base / "lib" / "patchright" / "driver",
        base / "lib" / "patchright" / "driver" / "package",
        base / "_internal" / "patchright" / "driver",
        base / "_internal" / "patchright" / "driver" / "package",
    ]
    for dll in candidates:
        if dll.is_dir():
            os.environ.setdefault("PATH", str(dll) + os.pathsep + os.environ.get("PATH", ""))


def _ensure_shardx_engine():
    """把随附的离线 ShardX 引擎解压/拷贝到 SDK 的 RUNTIME_DIR。

    打包时随附 <bundle>/shardx-sdk/ 完整目录 (ShardX-<平台>+widevine+fingerprints+
    manifest.json), 首次运行拷贝过去, 之后 install() 检测到 installed=True 即跳过下载
    (无网络也生效)。按平台匹配引擎目录名 (ShardX-Windows / ShardX-macOS / ShardX-Linux)。
    """
    try:
        from shardx.runtime import RUNTIME_DIR
    except Exception as e:
        _log(f"shardx.runtime 导入失败: {e!r}")
        return
    bundled = bundle_root() / "shardx-sdk"
    if not bundled.is_dir():
        _log("[desktop] 未随附离线 ShardX 引擎, 交由 SDK 联网下载")
        return
    target = Path(RUNTIME_DIR)
    engine_dir = "ShardX-Windows" if is_windows() else (
        "ShardX-macOS" if is_macos() else "ShardX-Linux")
    manifest = target / "manifest.json"
    if target.joinpath(engine_dir).is_dir() and manifest.exists():
        try:
            import json
            bm = json.loads((bundled / "manifest.json").read_text(encoding="utf-8"))
            lm = json.loads(manifest.read_text(encoding="utf-8"))
            if bm.get("installed_chromium_version") == lm.get("installed_chromium_version"):
                _log(f"[desktop] ShardX 引擎已就绪 ({engine_dir})")
                return
        except Exception:
            pass
    try:
        target.mkdir(parents=True, exist_ok=True)
        # 只拷引擎资产, 排除 profiles(运行时数据)/fingerprints(云端取)/下载残留
        for item in bundled.iterdir():
            if item.name in ("profiles", "fingerprints", ".tmp") or item.name.startswith("."):
                continue
            if item.is_dir():
                shutil.copytree(item, target / item.name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(
                                    "*.tmp", ".tmp", "*.download", "*.part"))
            else:
                shutil.copy2(item, target / item.name)
        # 指纹模板库不再随包附带(指纹数据源只走云端 fingerprint-db API)。但 SDK
        # install() 在 fingerprints/*.json 为空时会尝试联网下载 ShardX-Fingerprints.zip;
        # 离线异机会失败。写入一个极简占位 json, 让 SDK 判定"已有指纹"从而跳过联网下载。
        try:
            fp_dir = target / "fingerprints"
            if not list(fp_dir.glob("*.json")):
                fp_dir.mkdir(parents=True, exist_ok=True)
                (fp_dir / "sdk-offline-stub.json").write_text("{}", encoding="utf-8")
        except Exception as e2:
            _log(f"[desktop] fingerprints 占位写入失败(非致命): {e2!r}")
        _log(f"[desktop] 已安装离线 ShardX 引擎 → {target} ({engine_dir})")
    except Exception as e:
        _log(f"[desktop] 安装离线 ShardX 引擎失败: {e!r}")


def env_report() -> list[str]:
    """启动环境摘要, 写入日志便于远端排障 (异机打不开时第一现场)。"""
    import platform
    lines = []
    try:
        lines.append(f"OS: {platform.platform()}")
    except Exception:
        pass
    lines.append(f"Python: {sys.version.split()[0]} ({sys.executable})")
    try:
        from app.browser.cdp import ChromeLocator
        lines.append(f"Chrome: {ChromeLocator().find() or '(未找到, 将回退 Patchright Chromium)'}")
    except Exception as e:
        lines.append(f"Chrome 定位异常: {e!r}")
    try:
        from shardx.runtime import Runtime
        lines.append(f"ShardX 引擎: installed={bool(Runtime().installed)}")
    except Exception as e:
        lines.append(f"ShardX 检查异常: {e!r}")
    try:
        import patchright
        lines.append(f"patchright: {getattr(patchright, '__version__', '?')}")
    except Exception as e:
        lines.append(f"patchright 检查异常: {e!r}")
    lines.append(f"PLAYWRIGHT_BROWSERS_PATH: {os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '(未设置)')}")
    return lines


def check_env() -> list[str]:
    """环境检查, 返回缺失项列表 (空 = 全部就绪)。"""
    missing = []
    # 1) 系统 Chrome (channel=chrome 依赖)
    try:
        from app.browser.cdp import ChromeLocator
        if ChromeLocator().find() is None:
            missing.append("未检测到系统 Google Chrome (扫码登录/抓取需要)")
    except Exception as e:
        _log(f"Chrome 定位异常: {e!r}")
    # 2) ShardX 引擎
    try:
        from shardx.runtime import Runtime
        if not Runtime().installed:
            missing.append("ShardX 引擎未安装 (首次启动会自动安装/引导离线包)")
    except Exception as e:
        _log(f"ShardX 检查异常: {e!r}")
    return missing


_START_T0 = time.time()

_SPLASH_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<style>html,body{height:100%;margin:0;background:rgb(15,17,21);color:rgb(230,230,230);"
    "font-family:'Segoe UI',system-ui,sans-serif;"
    "display:flex;flex-direction:column;align-items:center;justify-content:center}"
    ".spinner{width:42px;height:42px;border:4px solid rgb(42,47,58);"
    "border-top-color:rgb(79,140,255);border-radius:50%;animation:sp 1s linear infinite}"
    "@keyframes sp{to{transform:rotate(360deg)}}"
    "h1{font-size:20px;font-weight:600;margin:20px 0 6px}"
    "p{color:rgb(138,147,166);font-size:13px;margin:0}</style></head><body>"
    "<div class='spinner'></div><h1>CreatorHub PRO</h1>"
    "<p>Starting local service...</p></body></html>"
)


def splash_url() -> str:
    """闪屏页以本地文件提供(file://), 不用 data: URI。

    实测: data:text/html 长 URI 在 Windows WebView2 里会被解析夹坏, 把 HTML 尾巴
    (如 'htm1>') 拼成导航 URL 导致先弹 404 错误页。写进可写目录再 file:// 打开稳定。
    """
    try:
        path = log_dir() / "splash.html"
        path.write_text(_SPLASH_HTML, encoding="utf-8")
        return path.as_uri()
    except Exception:
        # 兜底: 无法写文件时退回极简 data URI (纯 ASCII, 无注解字符)
        import urllib.parse
        return "data:text/html;charset=utf-8," + urllib.parse.quote(_SPLASH_HTML)


def _ensure_stdio():
    """windowed 打包 (无控制台) 时 sys.stdout/sys.stderr 为 None,
    uvicorn 初始化日志会调 stderr.isatty() 判色而崩溃 ('NoneType' has no attribute
    'isatty' -> Unable to configure formatter)。把缺失的流接到日志文件,
    既修复崩溃, 又能让 uvicorn 自身日志直接落进 desktop.log 便于异机排障。"""
    global _STDOUT_REDIRECTED
    if getattr(sys, "stdout", None) is not None and getattr(sys, "stderr", None) is not None:
        return
    try:
        stream = open(log_dir() / "desktop.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        stream = open(os.devnull, "w", encoding="utf-8")
    if getattr(sys, "stdout", None) is None:
        sys.stdout = stream
        _STDOUT_REDIRECTED = True
    if getattr(sys, "stderr", None) is None:
        sys.stderr = stream
        _STDOUT_REDIRECTED = True


def run_server(host: str, port: int, debug: bool):
    global _SERVER_ERROR
    # 直接 import app.main 的 app 对象传给 uvicorn —— PyInstaller 静态分析能看到
    # app 包并整体收集 (字符串 "app.main:app" 在 frozen 下会找不到)。
    try:
        _ensure_stdio()
        from app.main import app
        _log(f"[server] uvicorn 绑定 {host}:{port} (debug={debug})")
        uvicorn.run(app, host=host, port=port,
                    log_level="info" if debug else "warning",
                    access_log=debug)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _log(f"[server] uvicorn 启动失败: {e!r}\n{traceback.format_exc()}")
        _SERVER_ERROR = e


def wait_ready(url: str, timeout: float = 60.0) -> bool:
    """轮询 /health 直到后端就绪; 后端线程已死则立即失败。"""
    import urllib.request
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        if _SERVER_ERROR is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    if last_err is not None:
        _log(f"[desktop] /health 探测失败: {last_err!r}")
    return False


def _show_error(title: str, message: str):
    _log(f"[desktop] 弹窗: {title} | {message}")
    if is_windows():
        import ctypes
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            return
        except Exception:
            pass
    # 非 Windows 或 Windows 弹窗失败时打印 (DEBUG 控制台可见)
    print(f"[{title}] {message}", flush=True)


def gui_backend() -> str | None:
    """webview 后端: Windows 固定 edgechromium (WebView2, 现代 Chromium 渲染);
    macOS/Linux 交给 pywebview 自动探测 (macOS=cocoa/WKWebView)。"""
    if is_windows():
        return "edgechromium"
    return None  # 自动选择 (macOS -> cocoa, Linux -> gtk)


def main():
    debug = bool(os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
                 or "--debug" in sys.argv)
    _log(f"[desktop] main start (debug={debug}, args={sys.argv[1:]})")
    try:
        _main(debug)
    except Exception as e:
        import traceback
        _log("[desktop] main 异常:\n" + traceback.format_exc())
        _show_error("CreatorHub PRO 启动失败", f"{e}\n\n日志: {log_dir() / 'desktop.log'}")
        raise


def _main(debug: bool):
    _log("[desktop] init")
    _normalize_python_paths()
    _log("[desktop] env paths ok")

    host, port = read_server_bind()
    url = f"http://127.0.0.1:{port}"
    _log(f"[desktop] 服务地址: {url} (host={host}, 来自 config.yaml server.port)")

    # ① 后台起服务 (不等它; 窗口先用闪屏秒开)
    threading.Thread(target=run_server, args=(host, port, debug), daemon=True).start()

    import webview
    _log("[desktop] 创建窗口(闪屏即开) ...")
    window = webview.create_window(
        "CreatorHub PRO", splash_url(), width=1280, height=860,
        min_size=(1024, 700))
    _log("[desktop] webview.start...")
    try:
        # func 在 GUI 事件循环启动后于后台线程运行: 自检后移(③) + 就绪后跳转(②)
        webview.start(gui=gui_backend(), func=lambda: _loader(window, url),
                      debug=debug)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _log("[desktop] webview.start 异常:\n" + traceback.format_exc())
        _show_error("窗口启动失败",
                    f"WebView 启动失败: {e!r}\n\n"
                    f"Windows 请确认已安装 WebView2 Runtime:\n"
                    f"https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                    f"日志: {log_dir() / 'desktop.log'}")
        raise
    _log("[desktop] 窗口已关闭")
    _kill_leftover_engines()


def _kill_leftover_engines():
    """关窗退出前清理残留 ShardX 引擎进程树。

    应用的 uvicorn 在守护线程里, 窗口关闭进程即退出, 引擎子进程树可能存活并
    继续占用账号 profile 目录锁 → 下次启动 sdk.launch 起不来 (cdp_url 为空)。
    按命令行匹配 shardx-sdk 引擎路径, 杀整棵树 (不影响用户自己开的 ShardX Launcher)。
    """
    if not is_windows():
        return
    script = ("Get-CimInstance Win32_Process | Where-Object { "
              "$_.CommandLine -like '*shardx-sdk*ShardX*' } | "
              "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        _log(f"[desktop] 残留引擎扫描失败: {e!r}")
        return
    killed = 0
    for line in out.stdout.splitlines():
        pid = line.strip()
        if not pid.isdigit():
            continue
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", pid, "/T", "/F"],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            killed += 1
        except Exception:
            pass
    if killed:
        _log(f"[desktop] 已清理 {killed} 个残留 ShardX 引擎进程")


def _loader(window, url: str):
    """窗口打开后于后台线程执行: 不阻塞展示, 后台自检 + 就绪后跳转真实地址。"""
    # ③ 环境自检后移: 只写日志/弹一次性提示, 不挡窗口
    try:
        for line in env_report():
            _log(f"[env] {line}")
    except Exception as e:
        _log(f"[desktop] env_report 异常: {e!r}")
    _ensure_shardx_engine()
    try:
        missing = check_env()
        if missing:
            msg = "CreatorHub PRO 环境检查:\n\n" + "\n".join(
                f"  • {m}" for m in missing)
            _log("[desktop] 环境缺失:\n" + "\n".join(missing))
            _show_error("环境缺失", f"{msg}\n\n日志: {log_dir() / 'desktop.log'}")
    except Exception as e:
        _log(f"[desktop] check_env 异常: {e!r}")

    # ② 等后端就绪后跳转真实 URL (期间闪屏保持, 用户不干等白屏)
    if wait_ready(url, timeout=90):
        _log(f"[desktop] 服务就绪 {url} "
             f"(冷启动 {'{:.1f}'.format(time.time() - _START_T0)}s), 跳转窗口...")
        try:
            window.load_url(url)
        except Exception as e:
            _log(f"[desktop] load_url 失败: {e!r}")
    else:
        err = repr(_SERVER_ERROR) if _SERVER_ERROR else "启动超时(90s 内 /health 未就绪)"
        _show_error(
            "CreatorHub PRO 无法启动服务",
            f"无法访问 {url}\n\n后端错误: {err}\n\n"
            f"排查：\n"
            f"  1. 查看日志: {log_dir() / 'desktop.log'}\n"
            f"  2. 端口 {url.rsplit(':', 1)[-1]} 是否被占用: netstat -ano | findstr :{url.rsplit(':', 1)[-1]}\n"
            f"  3. 首次使用需已安装 WebView2 运行时 (Win10/11 一般自带)\n"
            f"  4. 缺少系统 Google Chrome 时请先安装或关闭杀毒拦截\n")


if __name__ == "__main__":
    main()