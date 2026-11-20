import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from tracker.config import Settings, Target
from tracker.db import Database, elapsed_ms, now_ms, running_since
from tracker.linear import LinearBrowse, LinearError, LinearIntegration, LinearSettings, tracker_target


ROOT = Path(__file__).parent
SESSION_AGE = 30 * 24 * 60 * 60
SESSION_COOKIE = "tracker_session"
LOGIN_COOKIE = "tracker_login"
REQUEST_KEY = re.compile(r"[A-Za-z0-9_-]{32}")
LOCAL_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?")
Csrf = Annotated[str, Form(max_length=64)]
Nonce = Annotated[str, Form(min_length=32, max_length=32)]
IntervalId = Annotated[int, PathParam(ge=1, le=2**63 - 1)]
TimerVersion = Annotated[int, Form(ge=0, le=2**63 - 1)]
TimerAction = Literal["pause", "resume"]


def linear_selection(
    team: UUID | None = None,
    project: UUID | Literal["none"] | None = None,
    q: Annotated[str, Query(max_length=100)] = "",
    after: Annotated[str, Query(max_length=512)] = "",
    before: Annotated[str, Query(max_length=512)] = "",
) -> LinearBrowse:
    if (project and not team) or (after and before):
        raise HTTPException(422)
    return LinearBrowse(str(team or ""), str(project or ""), q.strip(), after, before)


def clock(milliseconds: int) -> str:
    hours, seconds = divmod(max(0, milliseconds) // 1000, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def utc_datetime(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def parse_local(value: str, zone: ZoneInfo) -> int:
    if not LOCAL_DATETIME.fullmatch(value):
        raise ValueError("Укажите дату и время начала и окончания.")
    try:
        local = datetime.fromisoformat(value)
        candidates = set()
        for fold in (0, 1):
            utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates.add(utc)
    except (ValueError, OverflowError) as error:
        raise ValueError("Проверьте дату и время.") from error
    if len(candidates) != 1:
        raise ValueError("Выберите однозначное время вне перевода часов в этом часовом поясе.")
    return int(candidates.pop().timestamp()) * 1000


def agent_times(started: str, ended: str, zone: ZoneInfo) -> tuple[int, int]:
    start_ms = parse_local(started, zone)
    end_ms = parse_local(ended, zone)
    if end_ms <= start_ms:
        raise ValueError("Окончание должно быть позже начала.")
    return start_ms, end_ms


def create_app() -> FastAPI:
    settings = Settings.from_env()
    db = Database(settings.database, settings.database_url)
    linear_settings = LinearSettings.from_env()
    linear = LinearIntegration(db, linear_settings) if linear_settings else None
    password = hashlib.sha256(settings.password.encode()).digest()
    default_target = next(iter(settings.targets.values()))
    home_url = "/linear" if linear else default_target.url

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(db.initialize)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    static_dir = ROOT / "static"
    asset_versions = {
        path.relative_to(static_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for path in static_dir.rglob("*") if path.is_file()
    }
    templates.env.globals["asset"] = lambda name: f"/static/{name}?v={asset_versions[name]}"
    templates.env.filters.update(
        clock=clock,
        utc=lambda ms: utc_datetime(ms).isoformat(timespec="milliseconds"),
        local_time=lambda ms: utc_datetime(ms).astimezone(settings.timezone),
        elapsed_ms=elapsed_ms,
        running_since=running_since,
    )

    @app.middleware("http")
    async def private_headers(request: Request, call_next):
        response = await call_next(request)
        cache = "no-store"
        if request.url.path.startswith("/static/") and response.status_code in {200, 304}:
            name = request.url.path.removeprefix("/static/")
            version = asset_versions.get(name)
            cache = "public, max-age=86400" if name.endswith(".woff2") else "public, no-cache"
            if version and request.query_params.get("v") == version:
                cache = "public, max-age=31536000, immutable"
        response.headers["Cache-Control"] = cache
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session_digest(token: str) -> str:
        return hmac.new(password, token.encode(), "sha256").hexdigest()

    def csrf_token(token: str) -> str:
        return hashlib.sha256(f"csrf:{token}".encode()).hexdigest()

    def require_session(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        if REQUEST_KEY.fullmatch(token) and db.has_session(session_digest(token)):
            return token
        raise HTTPException(401)

    def require_post(csrf: Csrf, token: str = Depends(require_session)) -> str:
        if not hmac.compare_digest(csrf.encode(), csrf_token(token).encode()):
            raise HTTPException(403)
        return token

    def set_timer_playback(action: TimerAction, interval_id: int, version: int) -> None:
        if action == "pause":
            db.pause(interval_id, version)
        else:
            db.resume(interval_id, version)

    def restart_timer(interval_id: int, key: str) -> None:
        if not REQUEST_KEY.fullmatch(key):
            raise HTTPException(422)
        if not db.restart(interval_id, key):
            raise HTTPException(404, "Запись личного времени не найдена.")

    def timer_revision(active) -> str:
        return str(tuple((row["id"], row["version"]) for row in active))

    def focus_revision(focus) -> str:
        return hashlib.sha256(",".join(focus).encode()).hexdigest()[:16]

    def linear_targets():
        with db.transaction() as conn:
            return conn.execute("SELECT * FROM linear_targets ORDER BY kind DESC, name, id").fetchall()

    def all_targets(rows=None):
        imported = (tracker_target(row) for row in (linear_targets() if rows is None else rows))
        return {**settings.targets, **{target.slug: target for target in imported}}

    def get_target(slug: str) -> Target:
        target = settings.targets.get(slug)
        if target is None and slug.startswith("linear-"):
            with db.transaction() as conn:
                row = conn.execute("SELECT * FROM linear_targets WHERE id = ?", (slug[7:],)).fetchone()
            if row:
                return tracker_target(row)
        if target is None:
            raise HTTPException(404, "Цель не найдена")
        return target

    def set_cookie(response, name: str, token: str, age: int, same_site: str) -> None:
        response.set_cookie(
            name, token, max_age=age, httponly=True, secure=settings.secure_cookie, samesite=same_site
        )

    def render_login(
        request: Request, target: Target | None = None, *,
        selection: LinearBrowse = LinearBrowse(), error: str | None = None,
    ):
        nonce = token_urlsafe(24)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "page_title": "Вход",
                "login_url": f"{target.url}/login" if target else f"/linear/login{selection.query}",
                "nonce": nonce,
                "error": error,
            },
            status_code=401 if error else 200,
        )
        set_cookie(response, LOGIN_COOKIE, nonce, 600, "strict")
        return response

    def render_page(
        request: Request,
        target: Target,
        token: str,
        *,
        partial: bool = False,
        error: str | None = None,
        form: dict | None = None,
    ):
        active, personal, agents = db.snapshot()
        focus = db.focus_tasks() if linear else ()
        revision = ".".join((
            timer_revision(active),
            str(tuple(row["id"] for row in personal)),
            str(tuple(row["id"] for row in agents)),
            focus_revision(focus),
        ))
        if partial and request.headers.get("X-Timer-Revision") == revision:
            return Response(status_code=204, headers={"X-Tracker-Now": str(now_ms())})
        context = {
            "target": target,
            "targets": all_targets(),
            "linear_enabled": linear is not None,
            "journal_url": target.url,
            "timezone": settings.timezone.key,
            "csrf": csrf_token(token),
            "start_key": token_urlsafe(24),
            "active": active,
            "focus": focus,
            "personal": personal,
            "agents": agents,
            "running": next((row for row in active if row["target"] == target.slug), None),
            "now": now_ms(),
            "revision": revision,
            "error": error,
            "form": form or {"key": token_urlsafe(24), "started": "", "ended": ""},
        }
        return templates.TemplateResponse(
            request, "state.html" if partial else "target.html", context,
            status_code=422 if error else 200,
        )

    @app.get("/")
    def index():
        return RedirectResponse(home_url, status_code=303)

    @app.get("/t/{slug}", response_class=HTMLResponse)
    def page(request: Request, target: Target = Depends(get_target)):
        try:
            token = require_session(request)
        except HTTPException:
            return render_login(request, target)
        return render_page(request, target, token)

    def sign_in(
        request: Request, credential: str, nonce: str, target: Target | None = None, *,
        selection: LinearBrowse = LinearBrowse(),
    ):
        cookie = request.cookies.get(LOGIN_COOKIE, "")
        if not REQUEST_KEY.fullmatch(nonce) or not hmac.compare_digest(nonce.encode(), cookie.encode()):
            raise HTTPException(403)
        if not hmac.compare_digest(hashlib.sha256(credential.encode()).digest(), password):
            return render_login(request, target, selection=selection, error="Неверный пароль.")
        old_token = request.cookies.get(SESSION_COOKIE, "")
        if REQUEST_KEY.fullmatch(old_token):
            db.remove_session(session_digest(old_token))
        token = token_urlsafe(24)
        db.add_session(session_digest(token), now_ms() + SESSION_AGE * 1000)
        response = RedirectResponse(target.url if target else selection.url, status_code=303)
        set_cookie(response, SESSION_COOKIE, token, SESSION_AGE, "lax")
        response.delete_cookie(LOGIN_COOKIE, secure=settings.secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/t/{slug}/login")
    def login(
        request: Request,
        credential: Annotated[str, Form(max_length=4096)],
        nonce: Nonce,
        target: Target = Depends(get_target),
    ):
        return sign_in(request, credential, nonce, target)

    @app.post("/logout")
    def logout(token: str = Depends(require_post)):
        db.remove_session(session_digest(token))
        response = RedirectResponse(home_url, status_code=303)
        response.delete_cookie(SESSION_COOKIE, secure=settings.secure_cookie, httponly=True, samesite="lax")
        return response

    @app.get("/t/{slug}/state", response_class=HTMLResponse)
    def state(request: Request, target: Target = Depends(get_target), token: str = Depends(require_session)):
        return render_page(request, target, token, partial=True)

    @app.post("/t/{slug}/start")
    def start(key: Nonce, target: Target = Depends(get_target), token: str = Depends(require_post)):
        if not REQUEST_KEY.fullmatch(key):
            raise HTTPException(422)
        db.start(target, key)
        return RedirectResponse(target.url, status_code=303)

    @app.post("/t/{slug}/stop/{interval_id}")
    def stop(interval_id: IntervalId, target: Target = Depends(get_target), token: str = Depends(require_post)):
        db.stop(interval_id)
        return RedirectResponse(target.url, status_code=303)

    @app.post("/t/{slug}/timers/{action}/{interval_id}")
    def timer_playback(
        action: TimerAction, interval_id: IntervalId, version: TimerVersion,
        target: Target = Depends(get_target), token: str = Depends(require_post),
    ):
        set_timer_playback(action, interval_id, version)
        return RedirectResponse(target.url, status_code=303)

    @app.post("/t/{slug}/history/{interval_id}/restart")
    def restart_from_history(
        interval_id: IntervalId, key: Nonce, target: Target = Depends(get_target), token: str = Depends(require_post),
    ):
        restart_timer(interval_id, key)
        return RedirectResponse(target.url, status_code=303)

    @app.post("/t/{slug}/agent-runs")
    def record_agent_run(
        request: Request,
        key: Nonce,
        started: Annotated[str, Form(max_length=19)],
        ended: Annotated[str, Form(max_length=19)],
        target: Target = Depends(get_target),
        token: str = Depends(require_post),
    ):
        if target.kind != "issue":
            raise HTTPException(404)
        if not REQUEST_KEY.fullmatch(key):
            raise HTTPException(422)
        try:
            start_ms, end_ms = agent_times(started, ended, settings.timezone)
        except ValueError as error:
            return render_page(
                request, target, token, error=str(error),
                form={"key": key, "started": started, "ended": ended},
            )
        db.record_agent_run(target.slug, key, start_ms, end_ms)
        return RedirectResponse(target.url, status_code=303)

    if linear and linear_settings:
        @lru_cache(maxsize=4)
        def panel_workspace(bucket: int) -> str:
            return linear.workspace()

        @lru_cache(maxsize=128)
        def panel_target(kind: str, reference: str, bucket: int) -> tuple[Target, str]:
            return linear.resolve_target(kind, reference)

        def resolve_panel(url: str) -> tuple[Target, str]:
            try:
                parsed = urlsplit(url)
                workspace, kind, ref, *_ = parsed.path.strip("/").split("/")
                if (
                    parsed.scheme != "https" or parsed.netloc != "linear.app"
                    or kind not in {"issue", "project"}
                    or not re.fullmatch(r"[\w.-]{1,150}", unquote(ref))
                ):
                    raise ValueError
            except ValueError:
                raise HTTPException(422, "Откройте проект или задачу в Linear.") from None
            bucket = now_ms() // 300_000
            try:
                if workspace != panel_workspace(bucket):
                    raise HTTPException(403, "Эта карточка принадлежит другому рабочему пространству Linear.")
                return panel_target(kind, unquote(ref), bucket)
            except LinearError as error:
                raise HTTPException(502, str(error)) from None

        @app.get("/inline/state")
        def inline_state(url: Annotated[str, Query(max_length=900)], token: str = Depends(require_session)):
            target, _ = resolve_panel(url)
            active, personal, agents = db.snapshot()
            targets = all_targets()
            now = now_ms()

            def entry(row):
                item = targets.get(row["target"])
                return {
                    "id": row["id"],
                    "target": row["target"],
                    "name": item.name if item else row["target"],
                    "linearUrl": item.source_href if item else None,
                    "started": row["started_ms"],
                    "ended": row["ended_ms"],
                }

            return {
                "target": target,
                "active": [
                    {
                        **entry(row), "worked": row["worked_ms"] or 0,
                        "runningSince": running_since(row), "version": row["version"],
                    }
                    for row in active
                ],
                "personal": [{**entry(row), "worked": elapsed_ms(row, now)} for row in personal],
                "agents": [entry(row) for row in agents],
                "now": now,
                "timerControls": True,
                "timezone": settings.timezone.key,
                "csrf": csrf_token(token),
            }

        @app.get("/inline/project-issues")
        def inline_project_issues(
            url: Annotated[str, Query(max_length=900)],
            after: Annotated[str | None, Query(max_length=500)] = None,
            limit: Annotated[int, Query(ge=1, le=250)] = 8,
            token: str = Depends(require_session),
        ):
            project, _ = resolve_panel(url)
            if project.kind != "project":
                raise HTTPException(422, "Откройте проект в Linear.")
            try:
                return linear.project_issues(project.slug.removeprefix("linear-"), after, limit=limit)
            except LinearError as error:
                raise HTTPException(502, str(error)) from None

        @app.post("/inline/start", status_code=204)
        def inline_start(
            url: Annotated[str, Form(max_length=900)], key: Nonce,
            issue_id: Annotated[UUID | None, Form()] = None, token: str = Depends(require_post),
        ):
            if not REQUEST_KEY.fullmatch(key):
                raise HTTPException(422)
            target, reference = resolve_panel(url)
            if issue_id is not None:
                if target.kind != "project":
                    raise HTTPException(422, "Откройте проект в Linear.")
                try:
                    target, reference = linear.resolve_target(
                        "issue", str(issue_id), project_id=target.slug.removeprefix("linear-"),
                    )
                except LinearError as error:
                    raise HTTPException(409 if error.kind == "membership" else 502, str(error)) from None
            db.start(target, key, linear_reference=reference)

        @app.post("/inline/stop/{interval_id}", status_code=204)
        def inline_stop(interval_id: IntervalId, token: str = Depends(require_post)):
            db.stop(interval_id)

        @app.post("/inline/timers/{action}/{interval_id}", status_code=204)
        def inline_playback(
            action: TimerAction, interval_id: IntervalId, version: TimerVersion, token: str = Depends(require_post),
        ):
            set_timer_playback(action, interval_id, version)

        @app.post("/inline/history/{interval_id}/restart", status_code=204)
        def inline_restart(interval_id: IntervalId, key: Nonce, token: str = Depends(require_post)):
            restart_timer(interval_id, key)

        @app.post("/inline/agent-runs", status_code=204)
        def inline_agent(
            url: Annotated[str, Form(max_length=900)], key: Nonce,
            started: Annotated[str, Form(max_length=19)], ended: Annotated[str, Form(max_length=19)],
            token: str = Depends(require_post),
        ):
            if not REQUEST_KEY.fullmatch(key):
                raise HTTPException(422)
            target, reference = resolve_panel(url)
            if target.kind != "issue":
                raise HTTPException(404)
            try:
                start_ms, end_ms = agent_times(started, ended, settings.timezone)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            db.register_linear_target(target, reference)
            db.record_agent_run(target.slug, key, start_ms, end_ms)

        callback_errors = {
            "state": "Запрос авторизации устарел. Начните подключение заново.",
            "denied": "Linear не подтвердил подключение. Начните подключение заново.",
            "exchange": "Не удалось завершить подключение. Проверьте настройки OAuth и попробуйте снова.",
        }

        def linear_timer_state(token: str, selection: LinearBrowse, active, focus, rows=None) -> dict:
            return {
                "active": active,
                "targets": all_targets(rows),
                "now": now_ms(),
                "revision": timer_revision(active) + "." + focus_revision(focus),
                "focus": focus,
                "linear_enabled": True,
                "start_key": token_urlsafe(24),
                "csrf": csrf_token(token),
                "selection": selection,
            }

        def linear_navigation(selection: LinearBrowse, results: dict | None) -> dict:
            def menu(team: str) -> tuple[dict, ...]:
                if results and selection == LinearBrowse(team=team):
                    return tuple(results["page"]["nodes"])
                try:
                    return linear.browse(LinearBrowse(team=team))["page"]["nodes"]
                except LinearError:
                    return ()

            return {
                "menu_teams": menu(""),
                "menu_projects": menu(selection.team) if selection.team else (),
            }

        def linear_page(
            request: Request, token: str, *, selection: LinearBrowse = LinearBrowse(),
            error: str | None = None, message: str | None = None,
        ):
            connection = linear.connection()
            results = None
            browse_error = None
            if connection:
                try:
                    results = linear.browse(selection)
                except LinearError as failure:
                    browse_error = str(failure)
            page = results["page"]["pageInfo"] if results else {}
            focus = db.focus_tasks()
            rows = linear_targets()
            context = {
                **linear_timer_state(token, selection, db.active_timers(), focus, rows),
                **(linear_navigation(selection, results) if results else {}),
                "page_title": "Linear",
                "linear_enabled": True,
                "journal_url": default_target.url,
                "account_name": connection["account_name"] if connection else None,
                "saved_targets": [(tracker_target(row), row["reference"], row["id"]) for row in rows],
                "added": {row["id"]: tracker_target(row).url for row in rows},
                "results": results,
                "team_url": LinearBrowse(team=selection.team).url,
                "clear_url": LinearBrowse(team=selection.team, project=selection.project).url,
                "previous_url": (
                    replace(selection, after="", before=page["startCursor"]).url
                    if page.get("hasPreviousPage") and page.get("startCursor") else None
                ),
                "next_url": (
                    replace(selection, after=page["endCursor"], before="").url
                    if page.get("hasNextPage") and page.get("endCursor") else None
                ),
                "browse_error": browse_error,
                "tracker_origin": linear_settings.tracker_origin,
                "error": error,
                "message": message,
            }
            return templates.TemplateResponse(request, "linear.html", context, status_code=422 if error else 200)

        @app.get("/linear", response_class=HTMLResponse)
        def linear_home(request: Request, oauth: str = "", selection: LinearBrowse = Depends(linear_selection)):
            canonical_host = urlsplit(linear_settings.redirect_uri).netloc
            if request.headers.get("host") != canonical_host:
                return RedirectResponse(linear_settings.callback_origin + selection.url, status_code=303)
            try:
                token = require_session(request)
            except HTTPException:
                return render_login(request, selection=selection)
            return linear_page(request, token, selection=selection, error=callback_errors.get(oauth))

        @app.post("/linear/login")
        def linear_login(
            request: Request, credential: Annotated[str, Form(max_length=4096)], nonce: Nonce,
            selection: LinearBrowse = Depends(linear_selection),
        ):
            return sign_in(request, credential, nonce, selection=selection)

        @app.get("/linear/state", response_class=HTMLResponse)
        def linear_state(
            request: Request, selection: LinearBrowse = Depends(linear_selection), token: str = Depends(require_session),
        ):
            active = db.active_timers()
            focus = db.focus_tasks()
            if request.headers.get("X-Timer-Revision") == timer_revision(active) + "." + focus_revision(focus):
                return Response(status_code=204, headers={"X-Tracker-Now": str(now_ms())})
            return templates.TemplateResponse(request, "linear-state.html", linear_timer_state(token, selection, active, focus))

        @app.get("/linear/focus/search")
        def focus_search(
            q: Annotated[str, Query(max_length=100)] = "",
            token: str = Depends(require_session),
        ):
            try:
                return linear.search_issues(q.strip())
            except LinearError as error:
                raise HTTPException(502, str(error)) from None

        @app.post("/linear/focus")
        def focus_add(reference: Annotated[UUID, Form()], token: str = Depends(require_post)):
            try:
                target, identifier = linear.resolve_target("issue", str(reference))
            except LinearError as error:
                raise HTTPException(502, str(error)) from None
            db.add_focus_task(target, identifier)
            return {"url": target.url}

        @app.post("/linear/focus/{target_id}/remove", status_code=204)
        def focus_remove(target_id: UUID, token: str = Depends(require_post)):
            db.remove_focus_task(str(target_id))

        @app.get("/linear/source/{target_id}")
        def source_link(target_id: UUID, token: str = Depends(require_session)):
            target = get_target(f"linear-{target_id}")
            if not target.linear_url:
                try:
                    target = linear.import_target(target.kind, str(target_id))
                except LinearError as error:
                    raise HTTPException(502, str(error)) from None
            return RedirectResponse(target.linear_url, status_code=303)

        @app.get("/linear/authorize")
        def linear_authorize(request: Request, token: str = Depends(require_session)):
            if request.headers.get("host") != urlsplit(linear_settings.redirect_uri).netloc:
                return RedirectResponse(linear_settings.callback_origin + "/linear", status_code=303)
            return RedirectResponse(linear.authorize(session_digest(token)), status_code=303)

        @app.get("/oauth/callback")
        def linear_callback(
            state: str = "", code: str | None = None,
            token: str = Depends(require_session),
        ):
            try:
                linear.complete(session_digest(token), state, code)
            except LinearError as error:
                return RedirectResponse(f"/linear?oauth={error.kind}", status_code=303)
            panel_workspace.cache_clear()
            panel_target.cache_clear()
            linear._browse.cache_clear()
            linear._search_issues.cache_clear()
            return RedirectResponse("/linear", status_code=303)

        @app.post("/linear/targets")
        def linear_import_target(
            request: Request,
            kind: Annotated[Literal["project", "issue"], Form()],
            reference: Annotated[str, Form(min_length=1, max_length=100)],
            q: Annotated[str, Form(max_length=100)] = "",
            token: str = Depends(require_post),
        ):
            try:
                target = linear.import_target(kind, reference.strip())
            except LinearError:
                return linear_page(
                    request, token, selection=LinearBrowse(term=q.strip()),
                    error="Не удалось добавить таймер. Попробуйте ещё раз или переподключите Linear.",
                )
            return RedirectResponse(target.url, status_code=303)

        @app.post("/linear/targets/start")
        def linear_start(
            request: Request,
            kind: Annotated[Literal["project", "issue"], Form()],
            reference: Annotated[UUID, Form()],
            key: Nonce,
            selection: LinearBrowse = Depends(linear_selection),
            token: str = Depends(require_post),
        ):
            if (
                not REQUEST_KEY.fullmatch(key)
                or not selection.project
                or (kind == "project" and str(reference) != selection.project)
            ):
                raise HTTPException(422)
            try:
                target, resolved_reference = linear.resolve_target(
                    kind, str(reference),
                    project_id=selection.project if kind == "issue" else None,
                    team_id=selection.team if kind == "issue" else None,
                )
            except LinearError as error:
                return linear_page(request, token, selection=selection, error=str(error))
            db.start(target, key, linear_reference=resolved_reference)
            return RedirectResponse(selection.url, status_code=303)

        @app.post("/linear/stop/{interval_id}")
        def linear_stop(
            interval_id: IntervalId, selection: LinearBrowse = Depends(linear_selection), token: str = Depends(require_post),
        ):
            db.stop(interval_id)
            return RedirectResponse(selection.url, status_code=303)

        @app.post("/linear/timers/{action}/{interval_id}")
        def linear_playback(
            action: TimerAction, interval_id: IntervalId, version: TimerVersion,
            selection: LinearBrowse = Depends(linear_selection), token: str = Depends(require_post),
        ):
            set_timer_playback(action, interval_id, version)
            return RedirectResponse(selection.url, status_code=303)

        @app.post("/linear/targets/{target_id}/link")
        def linear_publish_link(
            request: Request, target_id: str, selection: LinearBrowse = Depends(linear_selection),
            token: str = Depends(require_post),
        ):
            if not linear_settings.tracker_origin:
                return linear_page(request, token, selection=selection, error="Сначала настройте общий HTTPS-адрес трекера.")
            with db.transaction() as conn:
                row = conn.execute("SELECT * FROM linear_targets WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise HTTPException(404)
            try:
                linear.publish_link(row)
            except LinearError as error:
                return linear_page(request, token, selection=selection, error=str(error))
            return linear_page(request, token, selection=selection, message=f"Ссылка «Таймер» сохранена в Linear для «{row['name']}».")

    return app
