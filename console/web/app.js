/* CreatorHub Console 前端(原生, 无构建链) */
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
    refreshInstances();
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

// ── 实例 ──
let INSTANCES = [];
async function refreshInstances() {
  try {
    const data = await api("/api/instances");
    INSTANCES = data.instances || [];
    $("inst-table").innerHTML = INSTANCES.map(i => `
      <tr class="inst-item ${i.id === CURRENT_INST ? "active" : ""}" data-id="${i.id}" onclick="selectInstance(${i.id})">
        <td>${esc(i.name)}</td><td><code>${esc(i.base_url)}</code></td>
        <td><span class="pill ${i.online ? "ok" : "bad"}">${i.online ? "在线" : "离线"}</span></td>
        <td><span class="pill ${i.token_ok ? "ok" : "warn"}">${i.token_ok ? "有效" : "待授权"}</span></td>
        <td style="font-size:12px;color:var(--dim)">${esc(i.last_error || "")}</td>
        <td>
          <button class="ghost" type="button" onclick="event.stopPropagation();reauthInstance(${i.id})">重新授权</button>
          <button class="danger" type="button" onclick="event.stopPropagation();deleteInstance(${i.id})">删除</button>
        </td>
      </tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}
async function addInstance() {
  const payload = {
    name: $("inst-name").value.trim(), base_url: $("inst-url").value.trim(),
    admin_username: $("inst-user").value.trim() || "admin",
    admin_password: $("inst-pass").value,
  };
  if (!payload.name || !payload.base_url || !payload.admin_password) {
    toast("名称/地址/实例密码必填", "err"); return;
  }
  try {
    const out = await api("/api/instances", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    $("inst-pass").value = "";
    toast("实例已注册: " + out.name, "ok");
    refreshInstances();
  } catch (e) { toast(e.message, "err"); }
}
async function reauthInstance(id) {
  const pass = prompt(`输入实例的 admin 密码（${INSTANCES.find(i => i.id === id)?.name}）`);
  if (!pass) return;
  try {
    await api(`/api/instances/${id}/reauth`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "admin", password: pass }) });
    toast("已重新授权", "ok");
    refreshInstances();
  } catch (e) { toast(e.message, "err"); }
}
async function deleteInstance(id) {
  if (!confirm("确认删除该实例注册？")) return;
  try {
    await api(`/api/instances/${id}`, { method: "DELETE" });
    if (CURRENT_INST === id) { CURRENT_INST = null; $("detail-card").hidden = true; }
    toast("已删除", "ok");
    refreshInstances();
  } catch (e) { toast(e.message, "err"); }
}

// ── 实例详情视图 ──
let CURRENT_INST = null;
function selectInstance(id) {
  CURRENT_INST = id;
  document.querySelectorAll(".inst-item").forEach(el =>
    el.classList.toggle("active", Number(el.dataset && el.dataset.id) === id));
  $("detail-card").hidden = false;
  const inst = INSTANCES.find(i => i.id === id);
  $("detail-title").textContent = `实例：${inst ? inst.name : id}`;
  vt("status");
  refreshStatus();
}
function vt(name) {
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.vi === name));
  ["status", "users", "risk", "areq", "aop"].forEach(v =>
    $(`vi-${v}`).classList.toggle("hidden", v !== name));
  if (name === "status") refreshStatus();
  if (name === "users") refreshRemoteUsers();
  if (name === "risk") refreshRemoteRisk();
  if (name === "areq") refreshRemoteAudit("areq");
  if (name === "aop") refreshRemoteAudit("aop");
}
function cur() { return CURRENT_INST; }
async function refreshStatus() {
  try {
    const st = await api(`/api/instances/${cur()}/status`);
    $("status-table").innerHTML = `
      <tr><td>在线</td><td><span class="pill ${st.online ? "ok" : "bad"}">${st.online ? "在线" : "离线"}</span></td></tr>
      <tr><td>账号数</td><td class="num">${st.account_count}</td></tr>
      <tr><td>监控目标</td><td class="num">${st.monitor_count}</td></tr>
      <tr><td>实例用户数</td><td class="num">${st.user_count}</td></tr>
      <tr><td>最近错误</td><td style="font-size:12px;color:var(--dim)">${esc(st.last_error || "—")}</td></tr>`;
  } catch (e) { toast(e.message, "err"); }
}
async function refreshRemoteUsers() {
  try {
    const data = await api(`/api/instances/${cur()}/users`);
    const self = authUser();
    $("users-table").querySelector("tbody").innerHTML = (data.users || []).map(u => `
      <tr>
        <td class="num">${u.id}</td>
        <td>${esc(u.username)}</td>
        <td><select onchange="remoteRole(${u.id}, this.value)">${["viewer", "operator", "admin"].map(r =>
        `<option value="${r}"${r === u.role ? " selected" : ""}>${r}</option>`).join("")}</select></td>
        <td><span class="pill ${u.enabled ? "ok" : "bad"}">${u.enabled ? "启用" : "停用"}</span></td>
        <td>
          <button class="ghost" onclick="remoteToggle(${u.id}, ${u.enabled})">${u.enabled ? "停用" : "启用"}</button>
          <button class="ghost" onclick="remoteReset(${u.id})">重置密码</button>
          <button class="danger" onclick="remoteDelete(${u.id}, '${esc(u.username)}')">删除</button>
        </td>
      </tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}
async function remoteCreateUser() {
  const name = $("ru-name").value.trim(), pass = $("ru-pass").value, role = $("ru-role").value;
  if (!name || pass.length < 8) { toast("用户名必填, 密码至少 8 位", "err"); return; }
  try {
    await api(`/api/instances/${cur()}/users`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, password: pass, role }) });
    $("ru-name").value = ""; $("ru-pass").value = "";
    toast("已在实例建号", "ok");
    refreshRemoteUsers();
  } catch (e) { toast(e.message, "err"); }
}
async function remoteRole(uid, role) {
  try { await api(`/api/instances/${cur()}/users/${uid}`, { method: "PATCH",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
    toast("角色已更新", "ok"); refreshRemoteUsers();
  } catch (e) { toast(e.message, "err"); }
}
async function remoteToggle(uid, enabled) {
  try { await api(`/api/instances/${cur()}/users/${uid}`, { method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !enabled }) });
    toast(enabled ? "已停用" : "已启用", "ok"); refreshRemoteUsers();
  } catch (e) { toast(e.message, "err"); }
}
async function remoteReset(uid) {
  const pw = prompt("新密码(至少 8 位)");
  if (!pw || pw.length < 8) return;
  try { await api(`/api/instances/${cur()}/users/${uid}/password`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_password: pw }) });
    toast("密码已重置", "ok");
  } catch (e) { toast(e.message, "err"); }
}
async function remoteDelete(uid, name) {
  if (!confirm(`确认在实例上删除用户 ${name}？`)) return;
  try { await api(`/api/instances/${cur()}/users/${uid}`, { method: "DELETE" });
    toast("已删除", "ok"); refreshRemoteUsers();
  } catch (e) { toast(e.message, "err"); }
}
async function refreshRemoteRisk() {
  try {
    const cfg = await api(`/api/instances/${cur()}/risk`);
    $("risk-json").value = JSON.stringify(cfg, null, 2);
  } catch (e) { toast(e.message, "err"); }
}
async function remotePutRisk() {
  let payload;
  try { payload = JSON.parse($("risk-json").value); }
  catch (e) { toast("JSON 解析失败: " + e.message, "err"); return; }
  try { await api(`/api/instances/${cur()}/risk`, { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    toast("风控配置已应用", "ok");
  } catch (e) { toast(e.message, "err"); }
}
async function refreshRemoteAudit(kind) {
  const t = kind === "areq" ? "areq-table" : "aop-table";
  const path = kind === "areq" ? "audit-requests" : "audit-ops";
  try {
    const rows = await api(`/api/instances/${cur()}/${path}?limit=100`) || [];
    $(t).querySelector("tbody").innerHTML = rows.map(r => kind === "areq"
      ? `<tr><td class="num">${new Date(r.created_at).toLocaleString()}</td><td>${esc(r.username || "—")}</td><td>${esc(r.method)}</td><td><code>${esc(r.path)}</code></td><td class="num">${r.status_code}</td></tr>`
      : `<tr><td class="num">${new Date(r.created_at).toLocaleString()}</td><td>${esc(r.action)}</td><td>${esc(r.actor)}</td><td>${esc((r.detail || "").slice(0, 80))}</td></tr>`).join("");
  } catch (e) { toast(e.message, "err"); }
}

// ── 启动 ──
(function boot() {
  if (!authToken()) { $("login-overlay").classList.add("on"); return; }
  api("/api/console/me").then(me => {
    setAuth(authToken(), me);
    renderChip();
    $("main").classList.remove("hidden");
    refreshInstances();
  }).catch(() => { $("login-overlay").classList.add("on"); });
})();