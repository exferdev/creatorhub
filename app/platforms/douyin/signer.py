"""抖音签名器 — Python V8 引擎运行 webmssdk SDK，生成 X-Bogus / a_bogus。

基于 douyin-ops 验证方案：py_mini_racer 嵌入 V8 → 浏览器环境补丁 → webmssdk.es5.js。
依赖: pip install py-mini-racer
"""
from __future__ import annotations

import json
import os
import time
import re
import threading
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs

try:
    from py_mini_racer import MiniRacer
    HAS_V8 = True
except Exception:
    HAS_V8 = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDK_PATH = os.path.join(SCRIPT_DIR, "webmssdk.es5.js")

BROWSER_SHIM_JS = r"""
var window = this;
var self = this;
var globalThis = this;

var navigator = {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
    appName: 'Netscape', appVersion: '5.0', platform: 'Win32',
    language: 'zh-CN', languages: ['zh-CN', 'zh', 'en'],
    cookieEnabled: true, hardwareConcurrency: 12, deviceMemory: 8,
    vendor: 'Google Inc.', maxTouchPoints: 0, webdriver: false, onLine: true,
};

var document = {
    cookie: '', referrer: 'https://www.douyin.com/',
    hidden: false, visibilityState: 'visible',
    dispatchEvent: function(){},
    addEventListener: function(){}, removeEventListener: function(){},
    createElement: function(tag) {
        if (tag === 'canvas') {
            return {
                tagName: 'CANVAS', style: {},
                getContext: function() { return null; },
                toDataURL: function() { return 'data:image/png;base64,'; },
                setAttribute: function(){}, getAttribute: function(){ return null; },
                appendChild: function(){}, removeChild: function(){},
                parentNode: null, children: [], childNodes: [],
                innerHTML: '', textContent: '', id: '', className: '',
            };
        }
        return {
            tagName: (tag || '').toUpperCase(), style: {},
            setAttribute: function(){}, getAttribute: function(){ return null; },
            appendChild: function(){}, removeChild: function(){},
            parentNode: null, children: [], childNodes: [],
            innerHTML: '', textContent: '', src: '', type: '', id: '', className: '',
        };
    },
    querySelector: function() { return null; },
    querySelectorAll: function() { return []; },
    getElementsByTagName: function() { return []; },
    getElementById: function() { return null; },
    head: { appendChild: function(){}, removeChild: function(){} },
    body: { appendChild: function(){}, removeChild: function(){} },
    createEvent: function() { return { initEvent: function(){} }; },
    documentElement: { style: {} },
    dispatchEvent: function(){},
};

var location = {
    href: 'https://www.douyin.com/jingxuan',
    host: 'www.douyin.com', hostname: 'www.douyin.com',
    protocol: 'https:', origin: 'https://www.douyin.com',
    pathname: '/jingxuan', search: '', hash: '', port: '',
    assign: function(){}, replace: function(){}, reload: function(){},
};
document.location = location;

var localStorage = {
    _data: {},
    getItem: function(k) { return this._data[k] || null; },
    setItem: function(k, v) { this._data[k] = String(v); },
    removeItem: function(k) { delete this._data[k]; },
    clear: function() { this._data = {}; },
};
var sessionStorage = {
    _data: {},
    getItem: function(k) { return this._data[k] || null; },
    setItem: function(k, v) { this._data[k] = String(v); },
    removeItem: function(k) { delete this._data[k]; },
    clear: function() { this._data = {}; },
};

var history = {
    length: 1, scrollRestoration: 'auto', state: null,
    pushState: function(){}, replaceState: function(){},
    back: function(){}, forward: function(){}, go: function(){},
};

var screen = {
    width: 1920, height: 1080, colorDepth: 24, pixelDepth: 24,
    availWidth: 1920, availHeight: 1040, availLeft: 0, availTop: 0,
};

var performance = {
    now: function() { return Date.now(); },
    timing: {
        navigationStart: Date.now() - 1000,
        domLoading: Date.now() - 800,
        domComplete: Date.now() - 200,
    },
    getEntriesByType: function() { return []; },
    getEntries: function() { return []; },
    mark: function(){}, measure: function(){},
};

var XMLHttpRequest = function() {
    this.UNSENT = 0; this.OPENED = 1; this.HEADERS_RECEIVED = 2;
    this.LOADING = 3; this.DONE = 4;
    this.readyState = 4; this.status = 200; this.statusText = 'OK';
    this.responseText = ''; this.response = '';
    this.responseType = ''; this.responseURL = ''; this.timeout = 0;
    this.withCredentials = false;
    this.onreadystatechange = null; this.onload = null;
    this.onerror = null; this.ontimeout = null;
    this.open = function open(){};
    this.send = function send(){};
    this.setRequestHeader = function setRequestHeader(){};
    this.getResponseHeader = function(){ return null; };
    this.getAllResponseHeaders = function(){ return ''; };
    this.abort = function(){};
    this.addEventListener = function(){};
    this.removeEventListener = function(){};
};

var __orig_toString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this === fetch || this === XMLHttpRequest || this === Request || this === Response ||
        this === XMLHttpRequest.prototype.open || this === XMLHttpRequest.prototype.send ||
        this === XMLHttpRequest.prototype.setRequestHeader) {
        return 'function ' + (this.name || '') + '() { [native code] }';
    }
    return __orig_toString.call(this);
};

var __captured_url = '';
var __captured_method = '';
var __captured_headers = {};
var __captured_body = '';

var __real_fetch = function fetch(input, init) {
    try {
        __captured_url = typeof input === 'string' ? input : (input.url || input.href || '');
        __captured_method = (init && init.method) || 'GET';
        __captured_headers = (init && init.headers) || {};
        __captured_body = (init && init.body) || '';
    } catch(e) { __captured_url = ''; }
    return Promise.resolve({
        ok: true, status: 200, statusText: 'OK', url: __captured_url,
        headers: {
            get: function(){ return null; },
            has: function(){ return false; },
            forEach: function(){},
        },
        json: function() { return Promise.resolve({}); },
        text: function() { return Promise.resolve('{}'); },
        blob: function() { return Promise.resolve({}); },
        arrayBuffer: function() { return Promise.resolve(new ArrayBuffer(0)); },
        clone: function() { return this; },
    });
};

var fetch = __real_fetch;

var Request = function(input, init) {
    this.url = typeof input === 'string' ? input : (input.url || '');
    this.method = (init && init.method) || 'GET';
    this.headers = init && init.headers || {};
    this.body = init && init.body || null;
    this.mode = (init && init.mode) || 'cors';
    this.credentials = (init && init.credentials) || 'same-origin';
};

var Response = function(body, init) {
    this.ok = true;
    this.status = (init && init.status) || 200;
    this.statusText = (init && init.statusText) || 'OK';
    this.url = '';
    this.body = body || null;
    this.headers = {
        get: function(){ return null; },
        has: function(){ return false; },
        forEach: function(){},
    };
    this.json = function() { return Promise.resolve({}); };
    this.text = function() { return Promise.resolve(''); };
};

var Headers = function(init) {
    this._headers = {};
    if (init) { for (var k in init) { this._headers[k.toLowerCase()] = init[k]; } }
};
Headers.prototype.get = function(k) { return this._headers[k.toLowerCase()] || null; };
Headers.prototype.has = function(k) { return k.toLowerCase() in this._headers; };
Headers.prototype.set = function(k, v) { this._headers[k.toLowerCase()] = v; };
Headers.prototype.append = function(k, v) {};
Headers.prototype.delete = function(k) { delete this._headers[k.toLowerCase()]; };
Headers.prototype.forEach = function(fn) { for (var k in this._headers) { fn(this._headers[k], k, this); } };

if (typeof atob === 'undefined') { var atob = function(s) { return s; }; }
if (typeof btoa === 'undefined') { var btoa = function(s) { return s; }; }

var console = {
    log: function(){}, warn: function(){}, error: function(){},
    info: function(){}, debug: function(){}, trace: function(){},
    dir: function(){}, table: function(){}, time: function(){}, timeEnd: function(){},
};

var __timeoutId = 0;
var __timeouts = {};
var setTimeout = function(fn, ms) {
    var id = ++__timeoutId;
    __timeouts[id] = { fn: fn, ms: ms, time: Date.now() };
    return id;
};
var setInterval = function(fn, ms) {
    var id = ++__timeoutId;
    __timeouts[id] = { fn: fn, ms: ms, interval: true, time: Date.now() };
    return id;
};
var clearTimeout = function(id) { delete __timeouts[id]; };
var clearInterval = function(id) { delete __timeouts[id]; };
var __flushTimeouts = function() {
    var now = Date.now();
    var ids = Object.keys(__timeouts);
    for (var i = 0; i < ids.length; i++) {
        var t = __timeouts[ids[i]];
        if (t && now - t.time >= t.ms) {
            try { t.fn(); } catch(e) {}
            if (t.interval) { t.time = now; }
            else { delete __timeouts[ids[i]]; }
        }
    }
};

var chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };

var Event = function(){};
var CustomEvent = function(){};
var MouseEvent = function(){};
var KeyboardEvent = function(){};
var PointerEvent = function(){};
var UIEvent = function(){};
var FocusEvent = function(){};
var WheelEvent = function(){};
var CompositionEvent = function(){};
var InputEvent = function(){};
var MessageEvent = function(){};
var ErrorEvent = function(){};
var ProgressEvent = function(){};
var PromiseRejectionEvent = function(){};

var Image = function() { this.src = ''; this.width = 0; this.height = 0; this.onload = null; this.onerror = null; };
var HTMLCanvasElement = function(){};
var Worker = function(){};

var FormData = function(){};
FormData.prototype.append = function(){};
FormData.prototype.delete = function(){};
FormData.prototype.get = function(){ return null; };
FormData.prototype.getAll = function(){ return []; };
FormData.prototype.has = function(){ return false; };
FormData.prototype.set = function(){};

var URLSearchParams = function(init) { this._params = init || ''; };

var Blob = function(parts, options) {
    this.size = 0;
    this.type = (options && options.type) || '';
};

var File = function(parts, name, options) {
    Blob.call(this, parts, options);
    this.name = name || '';
    this.lastModified = (options && options.lastModified) || Date.now();
};

if (typeof crypto === 'undefined') {
    var crypto = {
        getRandomValues: function(arr) {
            for (var i = 0; i < arr.length; i++) { arr[i] = Math.floor(Math.random() * 256); }
            return arr;
        },
        subtle: {},
    };
}

if (typeof TextEncoder === 'undefined') {
    var TextEncoder = function() {};
    TextEncoder.prototype.encode = function(s) {
        var arr = [];
        for (var i = 0; i < s.length; i++) {
            var c = s.charCodeAt(i);
            if (c < 0x80) { arr.push(c); }
            else if (c < 0x800) { arr.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f)); }
            else { arr.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f)); }
        }
        return new Uint8Array(arr);
    };
}
if (typeof TextDecoder === 'undefined') {
    var TextDecoder = function() {};
    TextDecoder.prototype.decode = function(arr) {
        var s = '';
        for (var i = 0; i < arr.length; i++) { s += String.fromCharCode(arr[i]); }
        return decodeURIComponent(s);
    };
}

var WebSocket = function(){};

var MutationObserver = function(fn) {
    this.observe = function(){};
    this.disconnect = function(){};
};

var IntersectionObserver = function(fn, opts) {
    this.observe = function(){};
    this.unobserve = function(){};
    this.disconnect = function(){};
};

var ResizeObserver = function(fn) {
    this.observe = function(){};
    this.unobserve = function(){};
    this.disconnect = function(){};
};

var getComputedStyle = function() { return {}; };
var HTMLElement = function(){};
var HTMLScriptElement = function(){};
var HTMLIFrameElement = function(){};
var SVGElement = function(){};

var Node = function(){};
Node.ELEMENT_NODE = 1;
Node.TEXT_NODE = 3;

var ShadowRoot = function(){};

var matchMedia = function() {
    return {
        matches: false, media: '', onchange: null,
        addEventListener: function(){}, removeEventListener: function(){},
    };
};

if (typeof queueMicrotask === 'undefined') {
    var queueMicrotask = function(fn) { Promise.resolve().then(fn); };
}
"""


class DouyinSigner:
    """V8 签名器：在 py_mini_racer 中运行完整 webmssdk SDK，无需浏览器。"""

    def __init__(self):
        if not HAS_V8:
            raise RuntimeError("py_mini_racer 未安装，请执行: pip install py-mini-racer")
        self._ctx = MiniRacer()
        self._ready = False
        self._init_v8()

    def _init_v8(self) -> bool:
        try:
            self._ctx.eval(BROWSER_SHIM_JS)
            if not os.path.exists(SDK_PATH):
                print(f"[signer] SDK 文件不存在: {SDK_PATH}")
                return False
            with open(SDK_PATH, "r", encoding="utf-8") as f:
                sdk_code = f.read()
            print(f"[signer] SDK 大小: {len(sdk_code)} 字符")
            self._ctx.eval(sdk_code)
            print("[signer] SDK 加载完成")
            verify_js = """
            (function(){try{var ac=window.byted_acrawler||byted_acrawler;if(!ac)return JSON.stringify({error:'not found'});return JSON.stringify({ready:typeof ac.frontierSign==='function',methods:Object.keys(ac)});}catch(e){return JSON.stringify({error:e.message||String(e)});}})();
            """
            verify_result = self._ctx.eval(verify_js)
            info = json.loads(verify_result)
            if info.get("error"):
                print(f"[!] SDK 验证失败: {info['error']}")
                return False
            self._ready = info.get("ready", False)
            print(f"[signer] {'[OK] 签名器就绪' if self._ready else '[FAIL] 签名器未就绪'}")
            if self._ready:
                init_js = """
                (function(){try{var ac=window.byted_acrawler||byted_acrawler;if(!ac||typeof ac.init!=='function')return JSON.stringify({error:'init not available'});var result=ac.init({aid:6383,enablePathList:['/aweme/v2/web/module/feed/','/web/api/media/aweme/create_v2/','/aweme/v1/post_assistant/fast_detect/poll','/aweme/v1/open/publish/limit_app_groups']});return JSON.stringify({hasResult:!!result,xxbg:!!(result&&result.options&&result.options.xxbg)});}catch(e){return JSON.stringify({error:e.message||String(e)});}})();
                """
                try:
                    init_result = self._ctx.eval(init_js)
                    print(f"[signer] SDK init 结果: {json.loads(init_result)}")
                except Exception as e:
                    print(f"[!] SDK init 失败: {e}")
                print("[signer] 刷新定时器以触发 SDK 异步初始化...")
                for _ in range(30):
                    self._ctx.eval("__flushTimeouts();")
                    time.sleep(0.5)
                print("[signer] 定时器刷新完成")
            return self._ready
        except Exception as e:
            print(f"[!] V8 初始化失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def set_environment(self, ua: str = "", platform: str = "") -> None:
        """签名前把 V8 沙箱里的 navigator.userAgent / platform 对齐到实际账号身份，
        避免签名内容和真实浏览器发出的请求头不一致而被风控识别。"""
        if not ua and not platform:
            return
        try:
            if ua:
                self._ctx.eval(f"navigator.userAgent = {json.dumps(ua)};")
            if platform:
                self._ctx.eval(f"navigator.platform = {json.dumps(platform)};")
        except Exception as e:
            print(f"[!] set_environment 失败: {e}")

    def frontier_sign(self, url: str = "", method: str = "GET", data: str = "",
                      ua: str = "", platform: str = "") -> Optional[str]:
        """生成 X-Bogus (HTTP Header 用)。"""
        if not self._ready:
            return None
        try:
            self.set_environment(ua, platform)
            args = json.dumps({"url": url, "method": method, "data": data if data else None})
            sign_js = f"""
            (function(){{try{{var ac=window.byted_acrawler||byted_acrawler;if(!ac||typeof ac.frontierSign!=='function')return JSON.stringify({{error:'frontierSign not available'}});var args={args};var r=ac.frontierSign(args);return JSON.stringify({{'X-Bogus':String(r?.['X-Bogus']||'')}});}}catch(e){{return JSON.stringify({{error:e.message||String(e)}});}}}})();
            """
            result = self._ctx.eval(sign_js)
            result_obj = json.loads(result)
            if result_obj.get("error"):
                return None
            return result_obj.get("X-Bogus", "") or None
        except Exception as e:
            print(f"[!] frontierSign 失败: {e}")
            return None

    def generate_a_bogus(self, url: str, method: str = "GET",
                         post_data: str = "", headers: dict = None,
                         cookies: str = "", ua: str = "", platform: str = "",
                         debug: bool = True) -> Optional[str]:
        """通过 SDK fetch 拦截器生成 a_bogus (URL 参数用)。"""
        if not self._ready:
            return None
        try:
            self.set_environment(ua, platform)
            self._ctx.eval("__flushTimeouts();")
            if cookies:
                cookie_js = f"document.cookie = {json.dumps(cookies)};"
                self._ctx.eval(cookie_js)
            if debug:
                try:
                    env_check = json.loads(self._ctx.eval(
                        "JSON.stringify({ua:navigator.userAgent,platform:navigator.platform,"
                        "cookieLen:(document.cookie||'').length})"
                    ))
                    print(f"[signer][a_bogus-debug] env: ua={env_check.get('ua','')[:60]}, "
                          f"platform={env_check.get('platform')}, cookie_len={env_check.get('cookieLen')} "
                          f"(passed_cookie_len={len(cookies)})")
                except Exception as e:
                    print(f"[signer][a_bogus-debug] env check failed: {e}")
            headers_json = json.dumps(headers or {})
            body_val = json.dumps(post_data) if post_data else 'null'
            gen_js = f"""
            (function(){{try{{__captured_url='';__captured_method='';__captured_headers={{}};__captured_body='';var _url={json.dumps(url)};var _init={{method:{json.dumps(method)},headers:{headers_json},body:{body_val},mode:'cors',credentials:'include',referrer:'https://www.douyin.com/'}};var _promise=fetch(_url,_init);var has_X_Bogus=__captured_url.indexOf('X-Bogus=')!==-1;return JSON.stringify({{captured_url:__captured_url,url_modified:__captured_url!==_url,has_X_Bogus:has_X_Bogus,original_url:_url,}});}}catch(e){{return JSON.stringify({{error:e.message||String(e)}});}}}})();
            """
            result = self._ctx.eval(gen_js)
            result_obj = json.loads(result)
            if debug:
                print(f"[signer][a_bogus-debug] url={url}")
                print(f"[signer][a_bogus-debug] method={method}, post_data_len={len(post_data or '')}, "
                      f"extra_headers={list((headers or {}).keys())}")
                print(f"[signer][a_bogus-debug] gen_js result: error={result_obj.get('error')}, "
                      f"url_modified={result_obj.get('url_modified')}, has_X_Bogus={result_obj.get('has_X_Bogus')}")
                print(f"[signer][a_bogus-debug] captured_url={result_obj.get('captured_url','')}")
            if result_obj.get("error") or not result_obj.get("captured_url"):
                return None
            if not result_obj.get("url_modified"):
                return None
            parsed = urlparse(result_obj["captured_url"])
            query_params = parse_qs(parsed.query)
            a_bogus_list = query_params.get('a_bogus', [])
            x_bogus_list = query_params.get('X-Bogus', [])
            ab = a_bogus_list[0] if a_bogus_list else ''
            xb = x_bogus_list[0] if x_bogus_list else ''
            if debug:
                print(f"[signer][a_bogus-debug] extracted a_bogus={ab[:24] if ab else None}, "
                      f"X-Bogus={xb[:24] if xb else None}")
            if ab:
                return ab
            if xb:
                return xb
            return None
        except Exception as e:
            print(f"[!] generate_a_bogus 失败: {e}")
            return None

    def is_ready(self) -> bool:
        return self._ready


_signer: Optional[DouyinSigner] = None
_ready = False
_last_error = ""
_init_lock = threading.Lock()
_init_started = False


def _bootstrap():
    global _signer, _ready, _last_error
    try:
        print("[signer] 初始化 V8 + webmssdk SDK (约 15 秒)...")
        _signer = DouyinSigner()
        _ready = _signer.is_ready()
        _last_error = ""
        if _ready:
            print("[signer] 就绪 — frontierSign + generate_a_bogus 可用")
        else:
            _last_error = "SDK loaded but signer not ready"
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        print(f"[signer] 初始化异常: {_last_error}")


def ensure_ready() -> bool:
    global _init_started
    if _ready:
        return True
    with _init_lock:
        if _init_started:
            return False
        _init_started = True
        threading.Thread(target=_bootstrap, daemon=True).start()
    return False


def sign(params: dict) -> dict:
    """统一签名入口。传入 {url, method, data, a_bogus, cookies, ua, platform}。

    ua/platform 应传账号真实浏览器身份的值(见 browser.identity.Identity.ua),
    保证签名内容和浏览器实际发出的请求头一致，避免被风控识别为环境不一致。
    """
    url = params.get("url", "")
    method = params.get("method", "GET")
    data = params.get("data", "")
    need_a_bogus = params.get("a_bogus", False)
    cookies = params.get("cookies", "")
    ua = params.get("ua", "")
    platform = params.get("platform", "")

    if not _signer or not _ready:
        return {"ok": False, "error": "signer not ready (initializing, retry in ~15s)", "ready": False}

    result = {"ok": True, "ready": True, "method": method}

    xb = _signer.frontier_sign(url, method, data, ua=ua, platform=platform)
    if xb:
        result["X-Bogus"] = xb
    else:
        result["X-Bogus"] = None

    if need_a_bogus and cookies:
        ab = _signer.generate_a_bogus(url, method, post_data=data, cookies=cookies,
                                      ua=ua, platform=platform)
        if ab:
            result["a_bogus"] = ab

    return result


def get_status() -> dict:
    return {"ready": _ready, "initializing": _init_started and not _ready,
            "available": _ready, "error": _last_error}
