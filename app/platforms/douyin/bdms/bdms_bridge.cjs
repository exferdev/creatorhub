/**
 * bdms a_bogus 生成桥 — Node.js 子进程 (CommonJS)
 */
var path = require('path');
var libDir = __dirname;

// 加载环境模拟
var envUtils = require('./env');

// 加载 bdms
require('./bdms');

// 恢复 process
if (envUtils && envUtils.restoreProcess) {
    envUtils.restoreProcess();
}

var captured_a_bogus = null;
var originalSet = URLSearchParams.prototype.set;
URLSearchParams.prototype.set = function(key, value) {
    if (key === 'a_bogus') {
        captured_a_bogus = value;
    }
    return originalSet.call(this, key, value);
};

if (window.bdms && window.bdms.init) {
    window.bdms.init({ aid: 6383 });
}
if (envUtils && envUtils.simulateMouseTrack) {
    envUtils.simulateMouseTrack({points: 20, duration: 500});
}

function get_a_bogus(url, uifid, method, body) {
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
    } catch (e) { return null; }
    return captured_a_bogus;
}

process.stdin.setEncoding('utf8');
var buffer = '';
process.stdin.resume();
process.stdin.on('data', function(chunk) {
    buffer += chunk;
    var lines = buffer.split('\n');
    buffer = lines.pop();
    lines.forEach(function(line) {
        if (!line.trim()) return;
        try {
            var req = JSON.parse(line);
            var ab = get_a_bogus(req.url || '', req.uifid || '', req.method, req.body);
            process.stdout.write(JSON.stringify(ab ? {ok:true, a_bogus: ab} : {ok:false, error:'sign failed'}) + '\n');
        } catch (e) {
            process.stdout.write(JSON.stringify({ok:false, error: e.message}) + '\n');
        }
    });
});

process.stderr.write(JSON.stringify({ok:true, ready:true}) + '\n');
