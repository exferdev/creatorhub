/**
 * 真机指纹采集 probe — 在真实 Chrome 上运行, 输出对齐 ShardX schema 的 JSON。
 * 用法: page.evaluate(probe_js) → 结果保存为 fingerprint-db/database/<name>.json
 */
async () => {
  const out = {};
  const nav = navigator;

  // ── navigator ──
  out.name = "pending";
  out.navigator = {
    language: nav.language,
    languages: [...nav.languages],
    user_agent: nav.userAgent,
    platform: nav.platform,
    platform_value: nav.platform,
    hardware_concurrency: nav.hardwareConcurrency,
    device_memory: typeof nav.deviceMemory !== "undefined" ? nav.deviceMemory : null,
    vendor: nav.vendor,
    max_touch_points: nav.maxTouchPoints,
  };
  // 平台版本 (UA-CH)
  try {
    const uad = await nav.userAgentData.getHighEntropyValues([
      "platformVersion", "architecture", "bitness", "uaFullVersion", "fullVersionList",
    ]);
    out.navigator.platform_version = uad.platformVersion || "";
    out.client_hints = {
      brand: (uad.brands?.[1]?.brand) || "",
      brand_version: (uad.brands?.[1]?.version) || "",
      platform_version: uad.platformVersion || "",
      architecture: uad.architecture || "",
      bitness: uad.bitness || "",
      mobile: uad.mobile || false,
      brand_full_version: uad.uaFullVersion || "",
    };
  } catch (e) {
    out.client_hints = { error: String(e).slice(0, 60) };
  }

  // ── screen / window ──
  out.screen = {
    width: screen.width, height: screen.height,
    avail_width: screen.availWidth, avail_height: screen.availHeight,
    color_depth: screen.colorDepth, pixel_depth: screen.pixelDepth,
    device_pixel_ratio: window.devicePixelRatio || 1,
    avail_left: screen.availLeft, avail_top: screen.availTop,
  };
  out.window = {
    outer_width: window.outerWidth, outer_height: window.outerHeight,
    inner_width: window.innerWidth, inner_height: window.innerHeight,
  };

  // ── WebGL 完整参数 (webgl1 + webgl2) ──
  const GL1_PARAMS = [
    "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE", "MAX_VIEWPORT_DIMS",
    "MAX_TEXTURE_SIZE", "MAX_CUBE_MAP_TEXTURE_SIZE", "MAX_RENDERBUFFER_SIZE",
    "MAX_VERTEX_ATTRIBS", "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_UNIFORM_VECTORS",
    "MAX_VARYING_VECTORS", "MAX_FRAGMENT_UNIFORM_VECTORS", "MAX_TEXTURE_IMAGE_UNITS",
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS", "SUBPIXEL_BITS", "RED_BITS", "GREEN_BITS",
    "BLUE_BITS", "ALPHA_BITS", "DEPTH_BITS", "STENCIL_BITS",
    "MAX_SAMPLES", "VERSION", "VENDOR", "RENDERER", "SHADING_LANGUAGE_VERSION",
    "MAX_TEXTURE_MAX_ANISOTROPY_EXT",
  ];
  const GL2_PARAMS = [
    "MAX_DRAW_BUFFERS", "MAX_COLOR_ATTACHMENTS", "MAX_VERTEX_UNIFORM_COMPONENTS",
    "MAX_VERTEX_UNIFORM_BLOCKS", "MAX_FRAGMENT_UNIFORM_COMPONENTS",
    "MAX_FRAGMENT_UNIFORM_BLOCKS", "MAX_UNIFORM_BUFFER_BINDINGS",
    "MAX_UNIFORM_BLOCK_SIZE", "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS",
    "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_TEXTURE_LOD_BIAS",
    "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS", "MAX_SERVER_WAIT_TIMEOUT",
    "MAX_ELEMENT_INDEX", "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_VERTEX_OUTPUT_COMPONENTS",
    "MAX_SAMPLE_MASK_WORDS", "MAX_COLOR_TEXTURE_SAMPLES", "MAX_DEPTH_TEXTURE_SAMPLES",
    "MAX_INTEGER_SAMPLES", "MAX_VARYING_COMPONENTS", "MAX_TEXTURE_IMAGE_UNITS",
  ];
  const gl = (() => {
    try { return document.createElement("canvas").getContext("webgl"); } catch (e) { return null; }
  })();
  const gl2 = (() => {
    try { return document.createElement("canvas").getContext("webgl2"); } catch (e) { return null; }
  })();
  if (gl) {
    const read = (name) => {
      try {
        const v = gl.getParameter(gl[name]);
        return Array.isArray(v) ? [...v] : v;
      } catch (e) { return null; }
    };
    const w = { vendor: "", renderer: "", vendor_masked: "", renderer_masked: "" };
    try {
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      w.vendor = String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL));
      w.renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
    } catch (e) {}
    w.vendor_masked = String(gl.getParameter(gl.VENDOR));
    w.renderer_masked = String(gl.getParameter(gl.RENDERER));
    w.max_texture_size = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    w.max_vertex_attribs = gl.getParameter(gl.MAX_VERTEX_ATTRIBS);
    w.extensions = gl.getSupportedExtensions() || [];
    // shader precision
    w.shader_precision = {};
    for (const st of [gl.VERTEX_SHADER, gl.FRAGMENT_SHADER]) {
      for (const pt of [gl.LOW_FLOAT, gl.MEDIUM_FLOAT, gl.HIGH_FLOAT,
                        gl.LOW_INT, gl.MEDIUM_INT, gl.HIGH_INT]) {
        try {
          const p = gl.getShaderPrecisionFormat(st, pt);
          w.shader_precision[`${st}_${pt}`] = [p.rangeMin, p.rangeMax, p.precision];
        } catch (e) {}
      }
    }
    w.params = {};
    for (const n of GL1_PARAMS) w.params[n] = read(n);
    out.webgl = w;
  } else {
    out.webgl = { error: "no webgl context" };
  }
  if (gl2) {
    const read2 = (name) => {
      try {
        const v = gl2.getParameter(gl2[name]);
        return Array.isArray(v) ? [...v] : v;
      } catch (e) { return null; }
    };
    const w2 = {};
    for (const n of GL2_PARAMS) w2[n] = read2(n);
    out.webgl2 = w2;
  }

  // ── WebGPU (若可用) ──
  out.webgpu = null;
  try {
    if (navigator.gpu && navigator.gpu.requestAdapter) {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) {
        const info = adapter.info || {};
        const lim = (adapter.limits || {});
        out.webgpu = {
          vendor: info.vendor || "", architecture: info.architecture || "",
          device: info.device || "", description: info.description || "",
          limits: Object.fromEntries(Object.entries(lim).map(([k, v]) => [k, Number(v)])),
        };
      }
    }
  } catch (e) { out.webgpu = { error: String(e).slice(0, 60) }; }

  // ── audio ──
  try {
    const actx = new AudioContext();
    out.audio = {
      sample_rate: actx.sampleRate,
      channel_count: actx.destination.channelCount,
    };
    // OfflineAudioContext 真实渲染特征 (振荡器+压缩器 → 频谱)
    const off = new OfflineAudioContext(1, 5000, 44100);
    const osc = off.createOscillator(); osc.type = "triangle"; osc.frequency.value = 10000;
    const comp = off.createDynamicsCompressor();
    comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12;
    comp.attack.value = 0; comp.release.value = 0.25;
    osc.connect(comp); comp.connect(off.destination);
    osc.start();
    const rendered = await off.startRendering();
    const d = rendered.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < d.length; i++) sum += d[i];
    out.audio.offline_sum = sum;
    out.audio.offline_threshold = comp.threshold.value;
    actx.close();
  } catch (e) { out.audio = { error: String(e).slice(0, 80) }; }

  // ── speech voices ──
  try {
    const voices = await new Promise((res) => {
      const v = speechSynthesis.getVoices();
      if (v.length) return res(v);
      speechSynthesis.onvoiceschanged = () => res(speechSynthesis.getVoices());
      setTimeout(() => res(speechSynthesis.getVoices()), 1500);
    });
    out.speech = {
      voices: voices.map((x) => ({
        name: x.name, lang: x.lang, local_service: x.localService,
        is_default: x.default,
      })),
    };
  } catch (e) { out.speech = { error: String(e).slice(0, 60) }; }

  // ── storage / connection ──
  try {
    const est = await navigator.storage.estimate();
    out.storage_estimate = { quota_gb: Math.round(est.quota / 1024 / 1024 / 1024) };
  } catch (e) {}
  if (navigator.connection) {
    out.connection = {
      effective_type: navigator.connection.effectiveType,
      downlink_mbps: navigator.connection.downlink,
      rtt_msec: navigator.connection.rtt,
      save_data: navigator.connection.saveData,
    };
  }
  try { out.webauthn = { uvpa: await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable() }; } catch (e) {}
  out.memory = { heap_size_limit: performance.memory ? performance.memory.jsHeapSizeLimit : null };
  return out;
}
