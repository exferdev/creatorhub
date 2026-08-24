"""Console 后台管理界面(starlette-admin, Django Admin 式)。

服务端渲染管理台: 客户端账号/审计/指令/用户 数据浏览·筛选·编辑由 ModelView
自动生成; 启停/重置密码/下发指令做成行内动作按钮(原首页控制面板保留)。
需要独立依赖栈(requirements-console.txt): fastapi(新版)+starlette-admin。
"""
from __future__ import annotations

import json
import os
import secrets
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette_admin import BaseAdmin
from starlette_admin.actions import row_action
from starlette_admin.auth import AdminUser, AuthProvider
from starlette_admin.contrib.sqla import ModelView
from starlette_admin.exceptions import ActionFailed, LoginFailed
from starlette_admin.views import CustomView

from .db import get_session
from .models import (AlgoKey, ClientAccount, ClientAudit, ClientCommand,
                     ConsoleAudit, ConsoleUser)

ADMIN_SECRET = os.environ.get("CONSOLE_ADMIN_SECRET") or secrets.token_hex(16)


class ConsoleAuthProvider(AuthProvider):
    """后台登录: 校验 Console 用户(admin/operator 可进), 会话保持。"""

    async def login(self, username: str, password: str, remember_me: bool,
                    request: Request, response: Any) -> Any:
        from fastapi_users.password import PasswordHelper
        from sqlmodel import select
        ph = PasswordHelper()
        with get_session() as s:
            u = s.exec(select(ConsoleUser).where(
                ConsoleUser.username == username)).first()
            if u is None or not u.is_active or u.role not in ("admin", "operator"):
                raise LoginFailed("用户名或密码错误")
            ok, _ = ph.verify_and_update(password, u.hashed_password)
            if not ok:
                raise LoginFailed("用户名或密码错误")
            role = u.role
        if remember_me:
            response.set_cookie("remember_me", "yes", max_age=30 * 86400)
        request.session.update({"console_user": username, "role": role})
        return response

    async def is_authenticated(self, request: Request) -> bool:
        return bool(request.session.get("console_user"))

    def get_admin_user(self, request: Request) -> Optional[AdminUser]:
        # 注意: starlette-admin 0.17 为同步接口
        name = request.session.get("console_user")
        if not name:
            return None
        return AdminUser(username=name)

    async def logout(self, request: Request, response: Any) -> Any:
        request.session.clear()
        return RedirectResponse("/admin")


class ClientAccountView(ModelView):
    """客户端账号: 列表/筛选/编辑; 敏感字段不展示不编辑; 行内启停/密码/指令。"""
    name = "客户端账号"
    label = "客户端账号"
    identity = "client"
    icon = "fa-solid fa-laptop"
    fields = ["id", "username", "note", "disabled", "version",
              "registered_at", "last_seen_at", "last_error", "status_json"]
    exclude_fields_from_list = ["status_json"]
    exclude_fields_from_edit = ["id", "username", "version", "registered_at",
                                "last_seen_at", "last_error", "status_json",
                                "password_hash", "client_token"]
    exclude_fields_from_create = list(fields)
    searchable_fields = ["username", "note"]
    sortable_fields = ["id", "username", "disabled", "last_seen_at"]
    page_size = 25
    row_actions = ["client_disable", "client_enable",
                   "client_reset_password", "client_send_risk"]

    def _get_acc(self, request: Request, pk: Any) -> ClientAccount:
        try:
            cid = int(pk)
        except (TypeError, ValueError):
            raise ActionFailed("无效的客户端 ID")
        with get_session() as s:
            acc = s.get(ClientAccount, cid)
        if acc is None:
            raise ActionFailed("客户端不存在")
        return acc

    def _audit(self, request: Request, acc: ClientAccount, action_name: str,
               detail: str = "", ok: bool = True):
        try:
            with get_session() as s:
                s.add(ConsoleAudit(
                    username=request.session.get("console_user", "") or "",
                    client_id=acc.id, client_name=acc.username,
                    action=action_name, ok=ok, detail=detail[:500]))
                s.commit()
        except Exception:
            pass

    @row_action(name="client_disable", text="停用",
                confirmation="确认停用该客户端？(下次轮询生效)",
                action_btn_class="btn-outline-danger")
    async def client_disable_action(self, request: Request, pk: Any) -> str:
        acc = self._get_acc(request, pk)
        with get_session() as s:
            a2 = s.get(ClientAccount, acc.id)
            if a2.disabled:
                raise ActionFailed("客户端已是停用状态")
            a2.disabled = True
            s.add(a2)
            s.commit()
            username = a2.username
        self._audit(request, acc, "client.disable", detail=f"停用 {username}")
        return f"客户端 {username} 已停用"

    @row_action(name="client_enable", text="启用",
                action_btn_class="btn-outline-success")
    async def client_enable_action(self, request: Request, pk: Any) -> str:
        acc = self._get_acc(request, pk)
        with get_session() as s:
            a2 = s.get(ClientAccount, acc.id)
            if not a2.disabled:
                raise ActionFailed("客户端已在运行状态")
            a2.disabled = False
            s.add(a2)
            s.commit()
            username = a2.username
        self._audit(request, acc, "client.enable", detail=f"启用 {username}")
        return f"客户端 {username} 已启用"

    @row_action(name="client_reset_password", text="重置密码",
                action_btn_class="btn-outline-primary",
                form="""
                <form>
                    <div class="mt-3">
                        <label class="form-label">新密码(至少 6 位)</label>
                        <input type="password" class="form-control"
                               name="new_password" autocomplete="new-password">
                    </div>
                </form>
                """)
    async def client_reset_password_action(self, request: Request,
                                           pk: Any) -> str:
        from fastapi_users.password import PasswordHelper
        data = await request.form()
        new_pw = str(data.get("new_password") or "")
        if len(new_pw) < 6:
            raise ActionFailed("新密码至少 6 位")
        acc = self._get_acc(request, pk)
        with get_session() as s:
            a2 = s.get(ClientAccount, acc.id)
            a2.password_hash = PasswordHelper().hash(new_pw)
            s.add(a2)
            s.commit()
            username = a2.username
        self._audit(request, acc, "client.password_reset",
                    detail=f"重置 {username} 密码")
        return f"客户端 {username} 密码已重置(本机登录按新密码)"

    @row_action(name="client_send_risk", text="下发风控指令",
                action_btn_class="btn-outline-warning",
                form="""
                <form>
                    <div class="mt-3">
                        <label class="form-label">风控配置 JSON(risk.set)</label>
                        <textarea class="form-control" rows="5" name="payload"
                                  placeholder='{"risk_control":{"enabled":true},"schedule":{}}'></textarea>
                    </div>
                </form>
                """)
    async def client_send_risk_action(self, request: Request, pk: Any) -> str:
        data = await request.form()
        raw = str(data.get("payload") or "")
        try:
            params = json.loads(raw)
        except Exception:
            raise ActionFailed("JSON 解析失败, 请检查格式")
        if not isinstance(params, dict):
            raise ActionFailed("指令参数必须是 JSON 对象")
        acc = self._get_acc(request, pk)
        with get_session() as s:
            cmd = ClientCommand(client_id=acc.id, client_name=acc.username,
                                op="risk.set",
                                params=json.dumps(params, ensure_ascii=False))
            s.add(cmd)
            s.commit()
            cid = cmd.id
            username = acc.username
        self._audit(request, acc, "client.command",
                    detail=f"下发 risk.set 至 {username}")
        return f"指令 #{cid} 已下发至 {username}(下次轮询执行)"


class ClientAuditView(ModelView):
    """客户端推送的审计(只读)。"""
    name = "客户端审计"
    label = "客户端审计"
    identity = "clientaudit"
    icon = "fa-solid fa-scroll"
    fields = ["id", "client_name", "kind", "action", "username",
              "detail", "ok", "created_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    exclude_fields_from_detail = ["id"]
    searchable_fields = ["client_name", "action", "username"]
    sortable_fields = ["id", "client_name", "created_at"]
    page_size = 50


class ClientCommandView(ModelView):
    """指令队列与回执(只读)。"""
    name = "指令记录"
    label = "指令记录"
    identity = "clientcmd"
    icon = "fa-solid fa-paper-plane"
    fields = ["id", "client_name", "op", "params", "status", "result",
              "created_at", "done_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    searchable_fields = ["client_name", "op", "status"]
    sortable_fields = ["id", "client_name", "status", "created_at"]
    page_size = 50


class ConsoleAuditView(ModelView):
    """控制台自身操作审计(谁对哪台客户端做了什么, 只读)。"""
    name = "操作审计"
    label = "操作审计"
    identity = "consoleaudit"
    icon = "fa-solid fa-user-shield"
    fields = ["id", "username", "client_name", "action", "ok", "detail",
              "created_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    exclude_fields_from_detail = ["id"]
    searchable_fields = ["username", "client_name", "action"]
    sortable_fields = ["id", "username", "client_name", "created_at"]
    page_size = 50


class ConsoleUserView(ModelView):
    """控制台用户(只读浏览; 建号/改密走现有 /api/admin/users)。"""
    name = "控制台用户"
    label = "控制台用户"
    identity = "consoleuser"
    icon = "fa-solid fa-users"
    fields = ["id", "username", "display_name", "role", "is_active",
              "is_superuser", "created_at", "last_login_at"]
    exclude_fields_from_edit = list(fields)
    exclude_fields_from_create = list(fields)
    searchable_fields = ["username", "display_name", "role"]
    sortable_fields = ["id", "username", "role", "created_at"]
    page_size = 25


class DashboardView(CustomView):
    """仪表盘: 总览统计 + 最近动态。"""

    def __init__(self):
        super().__init__(label="仪表盘", icon="fa-solid fa-gauge-high",
                         path="/", template_path="dashboard.html",
                         name="index")

    async def render(self, request: Request, templates) -> Response:
        from sqlalchemy import func, select
        session = request.state.session
        total = session.execute(select(func.count()).select_from(
            ClientAccount)).scalar_one()
        disabled = session.execute(select(func.count()).select_from(
            ClientAccount).where(ClientAccount.disabled.is_(True))).scalar_one()
        pending = session.execute(select(func.count()).select_from(
            ClientCommand).where(ClientCommand.status == "pending")).scalar_one()
        online = session.execute(select(func.count()).select_from(
            ClientAccount).where(ClientAccount.last_seen_at.is_not(None),
                                 ClientAccount.disabled.is_(False))).scalar_one()
        audits = session.execute(select(ConsoleAudit).order_by(
            ConsoleAudit.id.desc()).limit(10)).scalars().all()
        client_audits = session.execute(select(ClientAudit).order_by(
            ClientAudit.id.desc()).limit(8)).scalars().all()
        # 跨平台合计(M1: 解析各客户端 status_json.platform_stats)
        sums = {"accounts": 0, "works": 0, "comments": 0, "monitors": 0}
        platforms: dict = {}
        accs = session.execute(select(ClientAccount)).scalars().all()
        for a in accs:
            st = json.loads(a.status_json or "{}")
            for item in st.get("platform_stats") or []:
                if not isinstance(item, dict):
                    continue
                p = str(item.get("platform") or "unknown")
                plats = platforms.setdefault(p, {
                    "platform": p, "clients": 0, "accounts": 0,
                    "works": 0, "comments": 0})
                plats["clients"] += 1
                for k in ("accounts", "works", "comments", "monitors"):
                    v = int(item.get(k) or 0)
                    sums[k] += v
                    if k != "monitors":
                        plats[k] += v
        return templates.TemplateResponse(request=request,
                                          name=self.template_path,
                                          context={
            "title": self.title(request),
            "stats": {"clients": int(total), "online": int(online),
                      "disabled": int(disabled),
                      "pending_cmds": int(pending),
                      **{k: int(v) for k, v in sums.items()},
                      "platforms": list(platforms.values())},
            "audits": audits, "client_audits": client_audits,
        })


class GuideView(CustomView):
    """客户端接入指引(静态说明页)。"""

    def __init__(self):
        super().__init__(label="接入指引", icon="fa-solid fa-book",
                         path="/guide", template_path="guide.html")


class PasswordView(CustomView):
    """修改自身密码(GET 表单 / POST 处理, 成功后吊销旧令牌)。"""

    def __init__(self):
        super().__init__(label="修改密码", icon="fa-solid fa-key",
                         path="/password", template_path="password.html",
                         methods=["GET", "POST"])

    async def render(self, request: Request, templates) -> Response:
        from fastapi_users.password import PasswordHelper
        from sqlmodel import select as _select
        ctx = {"title": self.title(request), "msg": None, "ok": False}
        if request.method == "POST":
            form = await request.form()
            cur = str(form.get("current_password") or "")
            new = str(form.get("new_password") or "")
            confirm = str(form.get("confirm_password") or "")
            name = request.session.get("console_user", "")
            if len(new) < 8:
                ctx.update(msg="新密码至少 8 位")
            elif new != confirm:
                ctx.update(msg="两次输入的新密码不一致")
            elif new == cur:
                ctx.update(msg="新密码不能与当前密码相同")
            else:
                ph = PasswordHelper()
                with get_session() as s:
                    u = s.exec(_select(ConsoleUser).where(
                        ConsoleUser.username == name)).first()
                    if u is None or not ph.verify_and_update(
                            cur, u.hashed_password)[0]:
                        ctx.update(msg="当前密码不正确")
                    else:
                        u.hashed_password = ph.hash(new)
                        s.add(u)
                        s.commit()
                        # 吊销该用户全部登录令牌(需重新登录)
                        from .console_auth import SyncAccessTokenDatabase
                        await SyncAccessTokenDatabase(s).delete_all_for_user(u.id)
                        ctx.update(msg="密码已修改, 请用新密码重新登录", ok=True)
        return templates.TemplateResponse(request=request,
                                          name=self.template_path,
                                          context=ctx)


class AlgoCenterView(CustomView):
    """算法中心: 注册表/切换回滚 + 指标趋势 + 服务/客户端双健康 + 密钥管理。

    GET 拉取远程数据; POST 处理 switch / key_create / key_delete(操作落审计)。
    Admin-Key 来自 env CONSOLE_ALGO_ADMIN_KEY; 未配置时页面给出提示。
    """
    icon = "fa-solid fa-microchip"

    def __init__(self):
        super().__init__(label="算法中心", icon=self.icon,
                         path="/algo", template_path="algo.html",
                         methods=["GET", "POST"])

    def _ctx_base(self, request: Request) -> dict:
        return {
            "title": self.title(request), "msg": None, "ok": False,
            "algo_configured": bool(os.environ.get("CONSOLE_ALGO_ADMIN_KEY", "")),
            "catalog": {}, "health_algorithms": [], "health_ok": 0,
            "health_total": 0, "client_health": [], "keys": [],
            "history_json": "[]", "new_key_value": "",
        }

    async def render(self, request: Request, templates) -> Response:
        from .main import (_algo_client, _algo_metrics_sample)
        ctx = self._ctx_base(request)
        if request.method == "POST":
            form = await request.form()
            action = str(form.get("action") or "")
            try:
                if action == "switch":
                    out = await _algo_client().switch(
                        str(form.get("platform") or ""),
                        str(form.get("algorithm") or ""),
                        str(form.get("version") or ""))
                    ctx.update(msg=f"已切换: {out.get('platform')}/{out.get('algorithm')} → {out.get('version')}",
                               ok=True)
                    self._ar(request, "algo.switch",
                             f"{form.get('platform')}/{form.get('algorithm')} → {form.get('version')}")
                elif action == "key_create":
                    import secrets as _secrets
                    from .models import AlgoKey as _AK
                    key = _secrets.token_urlsafe(32)
                    with get_session() as s:
                        row = _AK(name=str(form.get("key_name") or "unnamed")[:40],
                                  key_value=key)
                        s.add(row)
                        s.commit()
                    ctx.update(msg="密钥已生成(见下方新密钥框)",
                               new_key_value=key, ok=True)
                    self._ar(request, "algo.key.create",
                             f"生成算法密钥 #{row.id}")
                elif action == "key_delete":
                    kid = int(form.get("key_id") or 0)
                    with get_session() as s:
                        row = s.get(AlgoKey, kid)
                        if row:
                            s.delete(row)
                            s.commit()
                    ctx.update(msg="密钥登记已删除", ok=True)
                    self._ar(request, "algo.key.delete", f"删除算法密钥 #{kid}")
                else:
                    ctx.update(msg=f"未知操作: {action}")
            except Exception as e:
                ctx.update(msg=f"操作失败: {e}")
        # 拉取远程数据
        try:
            client = _algo_client()
            cat = await client.catalog()
            ctx["catalog"] = (cat.get("catalog") or {})
            health = await client.health()
            ctx["health_algorithms"] = health.get("algorithms") or []
            ctx["health_ok"] = sum(1 for a in ctx["health_algorithms"] if a.get("ok"))
            ctx["health_total"] = len(ctx["health_algorithms"])
            snap = await client.metrics()
            ctx["history_json"] = json.dumps(
                await _algo_metrics_sample(snap), ensure_ascii=False)
        except Exception as e:
            ctx.update(msg=ctx["msg"] or f"算法服务不可达: {e}")
        # 客户端命中健康 + 密钥列表(本地)
        from .main import _algo_client as _unused  # noqa: F401
        with get_session() as s:
            from sqlmodel import select as _sel
            from .models import ClientAccount as _CA, AlgoKey as _AK2
            accs = s.exec(_sel(_CA)).all()
            from datetime import datetime as _dt, timedelta as _td
            from .main import POLL_INTERVAL as _PI
            out = []
            for a in accs:
                st = json.loads(a.status_json or "{}")
                sh = st.get("sign_health") or {}
                online = a.last_seen_at is not None and \
                    _dt.utcnow() - a.last_seen_at <= _td(seconds=_PI * 3)
                out.append({"client": a.username, "online": online,
                            "disabled": a.disabled, "sign_health": sh})
            ctx["client_health"] = out
            ctx["keys"] = [{
                "id": r.id, "name": r.name, "enabled": r.enabled,
                "key_prefix": (r.key_value or "")[:12] + "…",
            } for r in s.exec(_sel(_AK2).order_by(_AK2.id)).all()]
        return templates.TemplateResponse(request=request,
                                          name=self.template_path,
                                          context=ctx)

    def _ar(self, request: Request, action_name: str, detail: str = ""):
        """落控制台操作审计。"""
        try:
            from .models import ConsoleAudit as _CA
            with get_session() as s:
                s.add(_CA(
                    username=request.session.get("console_user", "") or "",
                    action=action_name, ok=True, detail=detail[:500]))
                s.commit()
        except Exception:
            pass


class DataCenterView(CustomView):
    """数据中心: 平台总览矩阵 + 客户端明细 + 7 天趋势 + CSV 导出。

    数据: 当前快照来自各客户端 status_json.platform_stats;
    趋势来自 ClientMetric(5 分钟桶, 按日聚合)。
    """
    icon = "fa-solid fa-database"

    def __init__(self):
        super().__init__(label="数据中心", icon=self.icon,
                         path="/data", template_path="data.html",
                         methods=["GET"])

    def _snapshot(self):
        """聚合 status_json.platform_stats → 矩阵 + 明细。"""
        from datetime import datetime as _dt, timedelta as _td
        from sqlmodel import select as _sel
        from .main import POLL_INTERVAL as _PI
        matrix: dict = {}
        detail: list = []
        with get_session() as s:
            accs = s.exec(_sel(ClientAccount).order_by(
                ClientAccount.id)).all()
            for a in accs:
                online = a.last_seen_at is not None and \
                    _dt.utcnow() - a.last_seen_at <= _td(seconds=_PI * 3)
                st = json.loads(a.status_json or "{}")
                pstats = st.get("platform_stats") or []
                for item in pstats:
                    if not isinstance(item, dict):
                        continue
                    platform = str(item.get("platform") or "unknown")
                    m = matrix.setdefault(platform, {
                        "platform": platform, "clients": 0, "accounts": 0,
                        "monitors": 0, "works": 0, "comments": 0,
                        "danmaku": 0, "downloads": 0})
                    m["clients"] += 1
                    for k in ("accounts", "monitors", "works", "comments",
                              "danmaku", "downloads"):
                        m[k] += int(item.get(k) or 0)
                    detail.append({
                        "client": a.username, "platform": platform,
                        "accounts": int(item.get("accounts") or 0),
                        "monitors": int(item.get("monitors") or 0),
                        "works": int(item.get("works") or 0),
                        "comments": int(item.get("comments") or 0),
                        "danmaku": int(item.get("danmaku") or 0),
                        "downloads": int(item.get("downloads") or 0),
                        "online": online,
                    })
        return list(matrix.values()), detail

    def _trend(self):
        """ClientMetric 按 (platform, 日期) 聚合近 7 天。"""
        from sqlmodel import func, select as _sel
        from .models import ClientMetric as _CM
        out = {"dates": [], "platforms": []}
        try:
            with get_session() as s:
                rows = s.exec(_sel(
                    _CM.platform, func.date(_CM.ts),
                    func.sum(_CM.accounts), func.sum(_CM.works),
                    func.sum(_CM.comments),
                ).group_by(_CM.platform, func.date(_CM.ts)).order_by(
                    func.date(_CM.ts))).all()
            day_map: dict = {}
            plat_map: dict = {}
            for platform, day, accounts, works, comments in rows:
                if day not in day_map:
                    day_map[day] = len(day_map)
                    out["dates"].append(day)
                plat_map.setdefault(platform, {})[day] = {
                    "accounts": int(accounts or 0),
                    "works": int(works or 0),
                    "comments": int(comments or 0),
                }
            out["platforms"] = [
                {"platform": p, "series": {
                    metric: [v.get(metric, 0) for v in
                             [vals.get(d, {}) for d in out["dates"]]]
                    for metric in ("accounts", "works", "comments")}}
                for p, vals in plat_map.items()]
        except Exception as e:
            print(f"[data] 趋势查询失败: {e!r}")
        return out

    async def render(self, request: Request, templates) -> Response:
        matrix, detail = self._snapshot()
        if request.query_params.get("export") == "1":
            import io
            buf = io.StringIO()
            buf.write("platform,clients,accounts,monitors,works,comments,danmaku,downloads\n")
            for row in matrix:
                buf.write(",".join(str(row.get(k, 0)) for k in
                                   ("platform", "clients", "accounts",
                                    "monitors", "works", "comments",
                                    "danmaku", "downloads")) + "\n")
            return Response(
                content=buf.getvalue().encode("utf-8-sig"),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition":
                         'attachment; filename="data-center.csv"'})
        return templates.TemplateResponse(
            request=request, name=self.template_path,
            context={"title": self.title(request), "matrix": matrix,
                     "detail": detail,
                     "trend_json": json.dumps(self._trend(),
                                              ensure_ascii=False)})


def build_admin(engine) -> BaseAdmin:
    from pathlib import Path
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    from starlette_admin.contrib.sqla import Admin as SqlaAdmin
    # 注意: 必须用 sqla 的 Admin(自动挂 SQLAlchemyMiddleware → request.state.session)
    tpl_dir = str(Path(__file__).resolve().parent / "admin_templates")
    admin = SqlaAdmin(
        engine,
        title="CreatorHub Console 后台",
        base_url="/admin",
        templates_dir=tpl_dir,
        index_view=DashboardView(),
        auth_provider=ConsoleAuthProvider(),
        middlewares=[Middleware(SessionMiddleware, secret_key=ADMIN_SECRET)],
    )
    admin.add_view(ClientAccountView(ClientAccount))
    admin.add_view(DataCenterView())
    admin.add_view(ClientAuditView(ClientAudit))
    admin.add_view(ClientCommandView(ClientCommand))
    admin.add_view(ConsoleAuditView(ConsoleAudit))
    admin.add_view(ConsoleUserView(ConsoleUser))
    admin.add_view(AlgoCenterView())
    admin.add_view(GuideView())
    admin.add_view(PasswordView())
    return admin