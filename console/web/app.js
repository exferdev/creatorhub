/* CreatorHub Console 前端(原生) — 客户端集中管理 */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (type === "err" ? " err" : type === "ok" ? " ok" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

const AUTH_KEY = "console_token", USER_KEY = "console_user";
const authToken = () => localStorage.getItem(AUTH_KEY) || "";
const authUser = () => { try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch (e) { return null; } };
function setAuth(token, user) {
  if (token) localStorage.setItem(AUTH_KEY, token); else localStorage.removeItem(AUTH_KEY);
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user)); else localStorage.removeItem(USER_KEY);
}
async function api(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const tok = authToken();
  if (tok && !headers.has("Authorization")) headers.set("Authorization", "Bearer " + tok);
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && !path.includes("/api/console/auth/")) {
    setAuth("", null);
    $("login-overlay").classList.add("on");
    $("login-msg").textContent = "登录已过期，请重新登录";
    throw new Error("未登录或登录已过期");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.status);
  return body;
}

async function doLogin(ev) {
  ev.preventDefault();
  const msg = $("login-msg"); if (msg) msg.textContent = "";
  try {
    const body = await api("/api/console/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: $("login-user").value.trim(),
        password: $("login-pass").value }).toString(),
    });
    setAuth(body.access_token, null);
    const me = await api("/api/console/me");
    setAuth(body.access_token, me);
    $("login-overlay").classList.remove("on");
    renderChip();
    $("main").classList.remove("hidden");
    refreshClients();
  } catch (e) { if (msg) msg.textContent = e.message; }
}
async function doLogout() {
  try { await api("/api/console/auth/logout", { method: "POST" }); } catch (e) {}
  setAuth("", null);
  location.reload();
}
function renderChip() {
  const u = authUser();
  if (!u) return;
  $("user-chip").hidden = false;
  $("user-chip").innerHTML =
    `<span>${esc(u.username)} <span style="color:var(--dim)">${esc(u.role || "")}</span></span>` +
    `<button onclick="doLogout()">退出</button>`;
}

let CL = [], CURRENT = null;
const canManage = () => { const u = authUser(); return u && (u.role === "admin" || u.role === "operator" || u.is_superuser); };

async function refreshClients() {
  try {
    const data = await api("/api/admin/clients");
    CL = data.clients || [];
    $("client-table").innerHTML = CL.length ? CL.map(c => `
      <tr class="clk ${c.id === CURRENT ? "active" : ""}" data-id="${c.id}" onclick="selectClient(${c.id})">
        <td>${esc(c.username)}</td>
        <td><span class="pill ${c.online ? "ok" : "bad"}">${c.online ? "在线" : "离线"}</span></td>
        <td><span class="pill ${c.disabled ? "bad" : "ok"}">${c.disabled ? "已停用" : "运行中"}</span></td>
        <td>${esc(c.version || "—")}</td>
        <td class="num">${c.last_seen_at ? new Date(c.last_seen_at).toLocaleTimeString() : "—"}</td>
        <td class="num">${c.pending_commands}</td>
        <td><button class="ghost" onclick="event.stopPropagation();selectClient(${c.id})">管理</button></td>
      </tr>`).join("")
      : `<tr><td colspan="7" style="color:var(--dim)">暂无客户端 — 客户端配置填入本控制台地址与账号密码后会自动出现</td></tr>`;
  } catch (e) { toast(e.message, "err"); }
}
function selectClient(id) {
  CURRENT = id;
  document.querySelectorAll(".clk").forEach(el =>
    el.classList.toggle("active", Number(el.dataset.id) === id));
  $("detail-card").hidden = false;
  const c = CL.find(x => x.id === id);
  $("detail-title").textContent = "客户端：" + (c ? c.username : id);
  $("toggle-btn").textContent = c && c.disabled ? "启用" : "停用";
  vt("status");
}
function vt(name) {
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.vi === name));
  ["status", "audit", "cmds"].forEach(v =>
    $(`vi-${v}`).classList.toggle("hidden", v !== name));
  if (name === "status") loadStatus();
  if (name === "audit") loadAudit();
  if (name === "cmds") loadCmds();
}
function cur() { const c = CL.find(x => x.id === CURRENT); return c ? c.username : ""; }
async function loadStatus() {
  try {
    const c = await api(`/api/admin/clients/${encodeURIComponent(cur())}`);
    const rows = [
      ["在线", c.online ? "在线" : "离线"],
      ["状态", c.disabled ? "已停用" : "运行中"],
      ["上次心跳", c.last_seen_at ? new Date(c.last_seen_at).toLocaleString() : "—"],
      ["版本", c.version || "—"],
      ["账号数", (c.status && c.status.accounts) ?? "—"],
      ["监控目标", (c.status && c.status.monitors) ?? "—"],
      ["最近错误", c.last_error || "—"],
    ];
    $("status-table").innerHTML = rows.map(r =>
      `<tr><td style="color:var(--dim)">${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}
async function loadAudit() {
  try {
    const rows = await api(`/api/admin/clients/${encodeURIComponent(cur())}/audit?limit=100`) || [];
    $("audit-table").querySelector("tbody").innerHTML = rows.map(r =>
      `<tr><td class="num">${new Date(r.created_at).toLocaleString()}</td>
        <td>${esc(r.kind)}</td><td><code>${esc(r.action)}</code></td>
        <td>${esc(r.username || "—")}</td>
        <td><span class="pill ${r.ok ? "ok" : "bad"}">${r.ok ? "正常" : "失败"}</span></td></tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}
async function loadCmds() {
  try {
    const rows = await api(`/api/admin/clients/${encodeURIComponent(cur())}/commands?limit=50`) || [];
    $("cmds-table").querySelector("tbody").innerHTML = rows.map(r =>
      `<tr><td class="num">${r.id}</td><td>${esc(r.op)}</td>
        <td><span class="pill ${r.status === "done" ? "ok" : r.status === "failed" ? "bad" : "warn"}">${esc(r.status)}</span></td>
        <td>${esc((r.result || "").slice(0, 60))}</td>
        <td class="num">${r.created_at ? new Date(r.created_at).toLocaleString() : "—"}</td></tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}
async function toggleClient() {
  if (!canManage()) { toast("权限不足", "err"); return; }
  const c = CL.find(x => x.id === CURRENT);
  const action = c && c.disabled ? "enable" : "disable";
  if (action === "disable" && !confirm(`确认停用客户端 ${c.username}？`)) return;
  try {
    await api(`/api/admin/clients/${encodeURIComponent(cur())}/${action}`, { method: "POST" });
    toast(action === "disable" ? "已停用(客户端下次轮询生效)" : "已启用", "ok");
    refreshClients();
    setTimeout(loadStatus, 400);
  } catch (e) { toast(e.message, "err"); }
}
async function resetPass() {
  if (!canManage()) { toast("权限不足", "err"); return; }
  const pw = $("rp-pass").value;
  if (pw.length < 6) { toast("密码至少 6 位", "err"); return; }
  try {
    await api(`/api/admin/clients/${encodeURIComponent(cur())}/reset-password`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: pw }) });
    $("rp-pass").value = "";
    toast("已重置(客户端本机登录将按新密码验证)", "ok");
  } catch (e) { toast(e.message, "err"); }
}
async function sendRiskCommand() {
  if (!canManage()) { toast("权限不足", "err"); return; }
  let params;
  try { params = JSON.parse($("cmd-params").value); }
  catch (e) { toast("JSON 解析失败: " + e.message, "err"); return; }
  try {
    const out = await api(`/api/admin/clients/${encodeURIComponent(cur())}/command`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "risk.set", params }) });
    toast(`指令已下发 (#${out.command_id}), 客户端下次轮询执行`, "ok");
    setTimeout(loadCmds, 400);
  } catch (e) { toast(e.message, "err"); }
}

(function boot() {
  if (!authToken()) { $("login-overlay").classList.add("on"); return; }
  api("/api/console/me").then(me => {
    setAuth(authToken(), me);
    renderChip();
    $("main").classList.remove("hidden");
    refreshClients();
  }).catch(() => { $("login-overlay").classList.add("on"); });
})();