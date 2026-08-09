/**
 * bdms a_bogus 生成桥 — Node.js 子进程, stdin/stdout JSON 通信
 * 用法: node bdms_bridge.mjs
 * 输入: {"url":"https://imapi.douyin.com/v1/message/send?msToken=...","uifid":"..."}
 * 输出: {"ok":true,"a_bogus":"..."} 或 {"ok":false,"error":"..."}
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);

const path = require('path');
const libDir = path.dirname(new URL(import.meta.url).pathname);

// 加载环境模拟
const envUtils = require(path.join(libDir, 'env'));

// 加载 bdms
require(path.join(libDir, 'bdms'));

// 恢复 process (bdms 加载后会隐藏它)
if (envUtils && envUtils.restoreProcess) {
    envUtils.restoreProcess();
}

// 钩住 URLSearchParams.prototype.set 来捕获 a_bogus
let captured_a_bogus = null;
const originalSet = URLSearchParams.prototype.set;
URLSearchParams.prototype.set = function(key, value) {
    if (key === 'a_bogus') {
        captured_a_bogus = value;
    }
    return originalSet.call(this, key, value);
};

// 初始化 bdms
if (window.bdms && window.bdms.init) {
    window.bdms.init({ aid: 6383 });
}

// 模拟一次鼠标轨迹
if (envUtils && envUtils.simulateMouseTrack) {
    envUtils.simulateMouseTrack({
        points: 20, startX: 100, startY: 200, endX: 800, endY: 500, duration: 500
    });
}

function get_a_bogus(url, uifid, method, body) {
    if (envUtils && envUtils.simulateMouseTrack) {
        envUtils.simulateMouseTrack({
            points: Math.floor(Math.random() * 10) + 15,
            duration: Math.floor(Math.random() * 300) + 400
        });
    }
    captured_a_bogus = null;
    method = (method || 'GET').toUpperCase();
    try {
        var xhr = new XMLHttpRequest();
        var invokeList = [
            {"args": [method, url, true], func: function () {}},
            {"args": ["Accept", "application/json,text/plain,*/*"], func: function () {}},
            {"args": ["uifid", uifid || ""]}
        ];
        if (method !== 'GET' && method !== 'HEAD') {
            invokeList.splice(2, 0, {
                "args": ["Content-Type", "application/x-www-form-urlencoded"],
                func: function () {}
            });
        }
        xhr.bdmsInvokeList = invokeList;
        xhr.send((method === 'GET' || method === 'HEAD') ? null : (body || null));
    } catch (e) {
        return null;
    }
    return captured_a_bogus;
}

// stdin/stdout JSON 通信
process.stdin.setEncoding('utf8');
let buffer = '';
process.stdin.on('data', (chunk) => {
    buffer += chunk;
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
        if (!line.trim()) continue;
        try {
            const req = JSON.parse(line);
            const url = req.url || '';
            const uifid = req.uifid || '';
            const method = req.method || 'GET';
            const body = req.body || null;
            const a_bogus = get_a_bogus(url, uifid, method, body);
            const resp = a_bogus
                ? { ok: true, a_bogus: a_bogus }
                : { ok: false, error: 'sign failed' };
            process.stdout.write(JSON.stringify(resp) + '\n');
        } catch (e) {
            process.stdout.write(JSON.stringify({ ok: false, error: e.message }) + '\n');
        }
    }
});

process.stderr.write(JSON.stringify({ ok: true, ready: true }) + '\n');
