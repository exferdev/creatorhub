"""账号设备/网络画像(Identity)。

多账号防关联的核心:每个账号一套**独立且永久固定**的浏览器画像 ——
持久化 profile 目录、固定 UA / 视口 / 时区、专属代理、确定性指纹种子。
画像在登录/建号时生成一次,之后不再变化(指纹漂移本身也是风控信号)。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# 真实机型 User-Agent 池(Windows/Mac Chrome,版本接近主流)。
# 一号选定一条后固定;切勿频繁变更。
UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

# 常见桌面分辨率(视口)
VIEWPORTS = [(1280, 800), (1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]

# 国内账号统一东八区,务必与代理 IP 地区一致(别 IP 在国内、时区在美洲)
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_LOCALE = "zh-CN"

# 主要城市经纬度池(用于 geolocation 伪造的兜底):没有代理 IP 实测坐标时,
# 按账号 seed 确定性挑一座城市 + 小抖动,保证 navigator.geolocation 返回的坐标
# ①落在中国境内(与东八区/代理一致) ②同账号每次一致 ③多账号彼此分散(不撞同一点)。
_CITY_GEO = [
    (39.9042, 116.4074),  # 北京
    (31.2304, 121.4737),  # 上海
    (23.1291, 113.2644),  # 广州
    (22.5431, 114.0579),  # 深圳
    (30.2741, 120.1551),  # 杭州
    (30.5728, 104.0668),  # 成都
    (34.3416, 108.9398),  # 西安
    (29.5630, 106.5516),  # 重庆
    (32.0603, 118.7969),  # 南京
    (30.5928, 114.3055),  # 武汉
    (38.9140, 121.6147),  # 大连
    (36.0671, 120.3826),  # 青岛
    (28.2282, 112.9388),  # 长沙
    (26.0745, 119.2965),  # 福州
    (24.4798, 118.0894),  # 厦门
    (43.8171, 125.3235),  # 长春
]


@dataclass
class Identity:
    """一个账号的完整浏览器画像。account_id=None 表示匿名(未绑定账号的公开抓取)。"""
    account_id: Optional[int]
    profile_dir: str
    identity_mode: str = "legacy"
    proxy: str = ""
    ua: str = ""
    viewport_w: int = 1280
    viewport_h: int = 800
    timezone_id: str = DEFAULT_TZ
    locale: str = DEFAULT_LOCALE
    fp_seed: str = ""
    # 绑定的 ShardX profile id(fpdb-xxx);非空则 launch 时 open_profile 复用,跳过 fp_seed 随机
    shardx_id: str = ""
    # ── 独立 Profile 指纹覆盖 (ShardX Launcher 式, douyin/shardx 引擎生效) ──
    fingerprint_name: str = ""     # 非空则加载指定 fingerprint-db profile(跳过 seed 选型)
    os: str = ""                   # 平台 mac/win/linux(空=宿主);决定 fingerprint 筛选
    ua_override: str = ""          # UA 覆盖(空=用 fingerprint 自带)
    cpu_cores: int = 0             # CPU 覆盖(0=自带)
    memory_gb: int = 0             # 内存覆盖(0=自带)
    tz_override: str = ""          # 时区覆盖(""=自带, "auto"=按代理 geo)
    language_override: str = ""    # 语言覆盖(""=自带, "auto"=按代理 geo)
    webrtc_mode: str = "auto"      # auto/block/tcp_only
    overrides: dict = None         # noise/geolocation/media_devices/network/navigator
    # geolocation 伪造坐标(与代理 IP 归属地/时区对齐)。0 表示未设定,由 seed 派生兜底。
    geo_lat: float = 0.0
    geo_lon: float = 0.0
    # 迁移桥:首次为存量账号创建持久 profile 时,把这些登录态 Cookie 注入进去。
    bridge_states: tuple = ()
    # 仅用于选择浏览器后端；放在末尾保持现有位置参数构造兼容。
    platform: str = ""

    @property
    def key(self):
        if self.account_id is not None:
            return self.account_id
        # Concurrent fresh-login flows each own a different temporary profile.
        # A single ``_anon`` key would reuse or close another login's Chrome.
        profile = os.path.normcase(str(Path(self.profile_dir).resolve()))
        digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
        return f"_anon:{digest}"

    @property
    def geolocation(self) -> dict:
        """返回 Playwright context 用的 geolocation 坐标(有实测坐标用实测,否则按 seed 派生)。"""
        lat, lon = self.geo_lat, self.geo_lon
        if not lat or not lon:
            lat, lon = derive_geo(self.fp_seed or "0")
        return {"latitude": lat, "longitude": lon, "accuracy": 60}

    @classmethod
    def from_account(cls, acc, profiles_root: str, default_ua: str) -> "Identity":
        pdir = acc.profile_dir or str(Path(profiles_root) / f"acc_{acc.id}")
        bridge = tuple(s for s in (getattr(acc, "storage_state", ""),
                                   getattr(acc, "creator_storage_state", "")) if s)
        return cls(
            account_id=acc.id, profile_dir=pdir,
            platform=getattr(acc, "platform", "") or "",
            identity_mode=getattr(acc, "identity_mode", "legacy") or "legacy",
            proxy=acc.proxy or "",
            ua=acc.ua or default_ua,
            viewport_w=acc.viewport_w or 1280, viewport_h=acc.viewport_h or 800,
            timezone_id=acc.timezone_id or DEFAULT_TZ,
            locale=acc.locale or DEFAULT_LOCALE,
            fp_seed=acc.fp_seed or seed_from_id(acc.id),
            shardx_id=getattr(acc, "shardx_id", "") or "",
            geo_lat=getattr(acc, "geo_lat", 0.0) or 0.0,
            geo_lon=getattr(acc, "geo_lon", 0.0) or 0.0,
            bridge_states=bridge,
        )


def seed_from_id(account_id) -> str:
    """没有显式种子时,用账号 id 派生一个稳定种子(保证同账号每次指纹一致)。"""
    return hashlib.md5(f"creatorhub-acc-{account_id}".encode()).hexdigest()


def derive_geo(seed: str) -> tuple:
    """按 seed 确定性派生一个中国境内经纬度(城市池挑一 + ±0.05° 抖动)。
    用于没有代理 IP 实测坐标时的 geolocation 兜底,保证同账号一致、多账号分散。"""
    rnd = random.Random(f"geo-{seed}")
    lat, lon = rnd.choice(_CITY_GEO)
    return (round(lat + rnd.uniform(-0.05, 0.05), 6),
            round(lon + rnd.uniform(-0.05, 0.05), 6))


def generate_identity_fields() -> dict:
    """生成一套全新的画像字段(建号/登录时调用一次,写库后永久固定)。"""
    seed = uuid.uuid4().hex
    rnd = random.Random(seed)
    w, h = rnd.choice(VIEWPORTS)
    return {
        "ua": rnd.choice(UA_POOL),
        "viewport_w": w, "viewport_h": h,
        "timezone_id": DEFAULT_TZ, "locale": DEFAULT_LOCALE,
        "fp_seed": seed,
    }


# 真实 GPU 的 WebGL vendor/renderer(按平台分,ANGLE/Metal 形态),按 seed 固定挑一条。
# 池子越大越好:号一多,多个账号撞同一条 GPU 串会成为弱关联信号,故覆盖主流独显/核显。
# 字符串取自真实 Chrome 的 UNMASKED_RENDERER_WEBGL 形态(含驱动版本尾巴,越像真机)。
_WEBGL_WIN = [
    # ── NVIDIA GeForce(GTX 16 / RTX 20/30/40 系)──
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3699)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3699)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3623)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4633)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4633)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4629)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4629)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    # ── Intel 核显(UHD / Iris Xe / Arc)──
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.100.9316)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.2115)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 750 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4502)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4577)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4952)"),
    # ── AMD Radeon(RX 500/5000/6000/7000 系)──
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.21921.1000)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 5700 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.21921.1000)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24002.92)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24002.92)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 7900 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24033.1003)"),
]
_WEBGL_MAC = [
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Max, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)"),
    ("Google Inc. (Intel Inc.)",
     "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics OpenGL Engine, OpenGL 4.1)"),
    ("Google Inc. (Intel Inc.)",
     "ANGLE (Intel Inc., Intel(R) UHD Graphics 630 OpenGL Engine, OpenGL 4.1)"),
]


def _platform_bits(ua: str):
    """由 UA 推断 navigator.platform / userAgentData.platform / WebGL 池(保持内部一致)。"""
    if "Mac OS" in ua:
        return "MacIntel", "macOS", _WEBGL_MAC
    if "Linux" in ua and "Android" not in ua:
        return "Linux x86_64", "Linux", _WEBGL_WIN
    return "Win32", "Windows", _WEBGL_WIN


def fingerprint_script(seed: str, ua: str = "") -> str:
    """基于 seed 确定性派生的指纹注入脚本(add_init_script),同账号每次一致。

    仅覆盖「属性」类指纹 (navigator.webdriver / hardwareConcurrency /
    deviceMemory / platform / userAgentData), 与 UA/Sec-CH-UA 三者一致。

    不覆盖任何原生「方法」 (canvas/getImageData、WebGL getParameter、
    Audio getChannelData): JS 层方法覆盖可被第三方检测识别为"人工修改"
    (toString/描述符/行为统计多层判定), 且确定性噪声可被剥离, 差异化无效。
    真实渲染由浏览器自身保证; 多账号防关联靠 代理IP + 属性seed差异化
    (可选: 按账号分配不同 Chrome channel 产生真实渲染差异)。
    """
    rnd = random.Random(seed)
    hw = rnd.choice([4, 6, 8, 12, 16])
    mem = rnd.choice([4, 8, 16])
    platform, ua_data_plat, _webgl_pool = _platform_bits(ua)
    v = "0"
    m = re.search(r"Chrome/(\d+)", ua or "")
    if m:
        v = m.group(1)
    is_edge = "Edg/" in ua
    if is_edge:
        brands_js = (f'[{{"brand":"Chromium","version":"{v}"}},'
                     f'{{"brand":"Microsoft Edge","version":"{v}"}},'
                     f'{{"brand":"Not?A_Brand","version":"99"}}]')
    else:
        brands_js = (f'[{{"brand":"Chromium","version":"{v}"}},'
                     f'{{"brand":"Google Chrome","version":"{v}"}},'
                     f'{{"brand":"Not?A_Brand","version":"99"}}]')
    return f"""
(() => {{
  const def = (o, k, v) => {{ try {{
    Object.defineProperty(o, k, {{get: () => v, configurable: true}});
  }} catch (e) {{}} }};
  def(navigator, 'webdriver', false);
  def(navigator, 'hardwareConcurrency', {hw});
  def(navigator, 'deviceMemory', {mem});
  def(navigator, 'platform', {json.dumps(platform)});
  // userAgentData 与 UA / Sec-CH-UA 三者一致
  try {{
    const brands = {brands_js};
    const uad = {{
      brands: brands, mobile: false, platform: {json.dumps(ua_data_plat)},
      getHighEntropyValues: (hints) => Promise.resolve({{
        brands: brands, mobile: false, platform: {json.dumps(ua_data_plat)},
        platformVersion: "10.0.0", architecture: "x86", bitness: "64",
        uaFullVersion: "{v}.0.0.0",
        fullVersionList: brands.map(b => ({{brand: b.brand, version: b.version + '.0.0.0'}})),
      }}),
    }};
    def(navigator, 'userAgentData', uad);
  }} catch (e) {{}}
}})();
"""
