import hashlib
import json
import os
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from secrets import token_urlsafe
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from tracker.config import Target
from tracker.db import Database, Row, now_ms


AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearError(Exception):
    def __init__(self, message: str, *, kind: str = "exchange"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class LinearBrowse:
    team: str = ""
    project: str = ""
    term: str = ""
    after: str = ""
    before: str = ""

    @property
    def query(self) -> str:
        params = {"team": self.team, "project": self.project, "q": self.term, "after": self.after, "before": self.before}
        query = urlencode({key: value for key, value in params.items() if value})
        return f"?{query}" if query else ""

    @property
    def url(self) -> str:
        return "/linear" + self.query


@dataclass(frozen=True)
class LinearSettings:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    tracker_origin: str

    @classmethod
    def from_env(cls):
        client_id = os.environ.get("LINEAR_CLIENT_ID", "")
        client_secret = os.environ.get("LINEAR_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("LINEAR_REDIRECT_URI", "")
        if not any((client_id, client_secret, redirect_uri)):
            return None
        if not all((client_id, client_secret, redirect_uri)):
            raise ValueError("Set LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET and LINEAR_REDIRECT_URI together")
        redirect = urlsplit(redirect_uri)
        if (
            not redirect.hostname or redirect.username is not None
            or redirect.path != "/oauth/callback" or redirect.query or redirect.fragment
            or (redirect.scheme != "https" and (redirect.scheme, redirect.hostname) != ("http", "localhost"))
        ):
            raise ValueError("LINEAR_REDIRECT_URI must use /oauth/callback on HTTPS or http://localhost")
        tracker_origin = os.environ.get("LINEAR_TRACKER_ORIGIN", "").rstrip("/")
        if tracker_origin:
            origin = urlsplit(tracker_origin)
            if (
                origin.scheme != "https" or not origin.hostname or origin.hostname in {"localhost", "127.0.0.1", "::1"}
                or origin.username is not None or origin.path or origin.query or origin.fragment
            ):
                raise ValueError("LINEAR_TRACKER_ORIGIN must be an HTTPS origin reachable from both devices")
        return cls(client_id, client_secret, redirect_uri, tracker_origin)

    @property
    def callback_origin(self) -> str:
        return self.redirect_uri.removesuffix("/oauth/callback")


def _post(url: str, payload: bytes, headers: dict) -> dict:
    request = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            data = json.load(response)
            if isinstance(data, dict):
                return data
            raise ValueError("Expected a JSON object")
    except HTTPError as error:
        if error.code in {400, 401, 403}:
            raise LinearError("Linear отклонил запрос. Проверьте доступ приложения или переподключите Linear.") from None
        raise LinearError("Linear временно не ответил. Попробуйте ещё раз.") from None
    except (URLError, TimeoutError, ValueError):
        raise LinearError("Linear временно не ответил. Попробуйте ещё раз.") from None


def _graphql(token: str, query: str, variables: dict) -> dict:
    response = _post(
        GRAPHQL_URL, json.dumps({"query": query, "variables": variables}).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    if response.get("errors") or not isinstance(response.get("data"), dict):
        raise LinearError("Linear не выполнил запрос. Проверьте ID объекта и права доступа.")
    return response["data"]


def tracker_target(row: Row) -> Target:
    return Target(slug=f"linear-{row['id']}", name=row["name"], kind=row["kind"])


class LinearIntegration:
    def __init__(self, db: Database, settings: LinearSettings):
        self.db = db
        self.settings = settings

    def authorize(self, session_digest: str) -> str:
        state = token_urlsafe(32)
        verifier = token_urlsafe(48)
        challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with self.db.transaction(write=True) as conn:
            conn.execute("DELETE FROM linear_oauth_requests WHERE expires_ms <= ?", (now_ms(),))
            conn.execute(
                "INSERT INTO linear_oauth_requests VALUES (?, ?, ?, ?)",
                (state, session_digest, verifier, now_ms() + 600_000),
            )
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": "read,write",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def complete(self, session_digest: str, state: str, code: str | None) -> None:
        with self.db.transaction(write=True) as conn:
            request = conn.execute(
                """
                DELETE FROM linear_oauth_requests
                WHERE state = ? AND session_digest = ? AND expires_ms > ?
                RETURNING verifier
                """,
                (state, session_digest, now_ms()),
            ).fetchone()
        if request is None:
            raise LinearError("Запрос авторизации устарел.", kind="state")
        if not code:
            raise LinearError("Linear не подтвердил подключение.", kind="denied")
        with self.db.linear_connection_lock():
            access, refresh, expiry = self._token(
                grant_type="authorization_code", code=code, redirect_uri=self.settings.redirect_uri,
                code_verifier=request["verifier"],
            )
            viewer = _graphql(access, "query { viewer { name } }", {})["viewer"]
            self._save_connection(access, refresh, expiry, viewer["name"])

    def _token(self, **grant) -> tuple[str, str, int]:
        payload = urlencode({
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            **grant,
        }).encode()
        response = _post(TOKEN_URL, payload, {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            access = response["access_token"]
            refresh = response["refresh_token"]
            expiry = now_ms() + int(response["expires_in"]) * 1000
        except (KeyError, TypeError, ValueError):
            raise LinearError("Linear не вернул данные подключения. Начните подключение заново.") from None
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise LinearError("Linear не вернул данные подключения. Начните подключение заново.")
        return access, refresh, expiry

    def _save_connection(self, access: str, refresh: str, expiry: int, name: str) -> None:
        with self.db.transaction(write=True) as conn:
            conn.execute(
                """
                INSERT INTO linear_connection VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    client_id = excluded.client_id, access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token, expires_ms = excluded.expires_ms,
                    account_name = excluded.account_name
                """,
                (self.settings.client_id, access, refresh, expiry, name),
            )

    def connection(self) -> Row | None:
        with self.db.transaction() as conn:
            return conn.execute(
                "SELECT * FROM linear_connection WHERE id = 1 AND client_id = ?",
                (self.settings.client_id,),
            ).fetchone()

    def access_token(self) -> str:
        known = self.connection()
        if known is not None and known["expires_ms"] > now_ms() + 60_000:
            return known["access_token"]
        with self.db.linear_connection_lock():
            known = self.connection()
            if known is None:
                raise LinearError("Сначала подключите аккаунт Linear.")
            if known["expires_ms"] > now_ms() + 60_000:
                return known["access_token"]
            access, refresh, expiry = self._token(grant_type="refresh_token", refresh_token=known["refresh_token"])
            self._save_connection(access, refresh, expiry, known["account_name"])
            return access

    def browse(self, selection: LinearBrowse) -> dict:
        pagination = {
            "first": None if selection.before else 25,
            "last": 25 if selection.before else None,
            "after": selection.after or None,
            "before": selection.before or None,
        }
        name_filter = {"containsIgnoreCase": selection.term}
        token = self.access_token()
        if not selection.team:
            return _graphql(
                token,
                """
                query TrackerTeams($filter: TeamFilter, $first: Int, $last: Int, $after: String, $before: String) {
                    page: teams(filter: $filter, first: $first, last: $last, after: $after, before: $before) {
                        nodes { id name key }
                        pageInfo { hasNextPage endCursor hasPreviousPage startCursor }
                    }
                }
                """,
                {**pagination, "filter": {"name": name_filter} if selection.term else None},
            )
        if not selection.project:
            data = _graphql(
                token,
                """
                query TrackerProjects($team: String!, $filter: ProjectFilter,
                                      $first: Int, $last: Int, $after: String, $before: String) {
                    team(id: $team) {
                        name
                        page: projects(filter: $filter, first: $first, last: $last,
                                       after: $after, before: $before, orderBy: updatedAt) {
                            nodes { id name }
                            pageInfo { hasNextPage endCursor hasPreviousPage startCursor }
                        }
                    }
                }
                """,
                {**pagination, "team": selection.team, "filter": {"name": name_filter} if selection.term else None},
            )
            return {"team": data["team"]["name"], "page": data["team"]["page"]}
        issue_filter = {"title": name_filter} if selection.term else {}
        if selection.project == "none":
            data = _graphql(
                token,
                """
                query TrackerUnassigned($team: String!, $filter: IssueFilter!,
                                        $first: Int, $last: Int, $after: String, $before: String) {
                    team(id: $team) {
                        name
                        page: issues(filter: $filter, first: $first, last: $last,
                                     after: $after, before: $before, orderBy: updatedAt) {
                            nodes { id name: title identifier }
                            pageInfo { hasNextPage endCursor hasPreviousPage startCursor }
                        }
                    }
                }
                """,
                {**pagination, "team": selection.team, "filter": {**issue_filter, "project": {"null": True}}},
            )
            return {"team": data["team"]["name"], "project": "Без проекта", "page": data["team"]["page"]}
        team_filter = {"id": {"eq": selection.team}}
        data = _graphql(
            token,
            """
            query TrackerIssues($team: String!, $project: String!, $membership: TeamFilter!, $filter: IssueFilter!,
                                $first: Int, $last: Int, $after: String, $before: String) {
                team(id: $team) { name }
                project(id: $project) {
                    name
                    teams(first: 1, filter: $membership) { nodes { id } }
                    page: issues(filter: $filter, first: $first, last: $last,
                                 after: $after, before: $before, orderBy: updatedAt) {
                        nodes { id name: title identifier }
                        pageInfo { hasNextPage endCursor hasPreviousPage startCursor }
                    }
                }
            }
            """,
            {
                **pagination, "team": selection.team, "project": selection.project,
                "membership": team_filter, "filter": {**issue_filter, "team": team_filter},
            },
        )
        project = data["project"]
        if not project["teams"]["nodes"]:
            raise LinearError("Этот проект больше не относится к выбранной команде.", kind="membership")
        return {"team": data["team"]["name"], "project": project["name"], "page": project["page"]}

    def workspace(self) -> str:
        return _graphql(self.access_token(), "query { organization { urlKey } }", {})["organization"]["urlKey"]

    def project_issues(self, project_id: str, after: str | None, *, limit: int = 8) -> dict:
        data = _graphql(
            self.access_token(),
            """
            query ($id: String!, $after: String, $limit: Int!) {
                project(id: $id) {
                    issues(first: $limit, after: $after, orderBy: updatedAt) {
                        nodes { id identifier title }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            }
            """,
            {"id": project_id, "after": after, "limit": limit},
        )
        return data["project"]["issues"]

    def resolve_target(
        self, kind: str, reference: str, *, project_id: str | None = None, team_id: str | None = None,
    ) -> tuple[Target, str]:
        if kind == "project":
            query = "query ($id: String!) { project(id: $id) { id name } }"
            name_field = "name"
        else:
            query = "query ($id: String!) { issue(id: $id) { id identifier title project { id } team { id } } }"
            name_field = "title"
        data = _graphql(self.access_token(), query, {"id": reference}).get(kind)
        if not data:
            raise LinearError("Linear не нашёл эту задачу или проект.")
        expected_project = None if project_id == "none" else project_id
        if project_id is not None and (data.get("project") or {}).get("id") != expected_project:
            raise LinearError("Задача больше не находится в выбранном разделе.", kind="membership")
        if team_id and (data.get("team") or {}).get("id") != team_id:
            raise LinearError("Задача больше не относится к выбранной команде.", kind="membership")
        target_id = str(UUID(data["id"]))
        target = Target(slug=f"linear-{target_id}", kind=kind, name=data[name_field])
        return target, data.get("identifier", reference)

    def import_target(self, kind: str, reference: str) -> Target:
        target, resolved_reference = self.resolve_target(kind, reference)
        self.db.register_linear_target(target, resolved_reference)
        return target

    def publish_link(self, row: Row) -> None:
        token = self.access_token()
        target = tracker_target(row)
        url = self.settings.tracker_origin + target.url
        if target.kind == "issue":
            data = _graphql(
                token, "mutation ($input: AttachmentCreateInput!) { attachmentCreate(input: $input) { success } }",
                {"input": {"issueId": row["id"], "title": "Таймер", "url": url}},
            )
            success = data["attachmentCreate"]["success"]
        else:
            success = self._publish_project_link(token, row, url)
        if not success:
            raise LinearError("Linear не сохранил ссылку. Попробуйте ещё раз.")

    def _publish_project_link(self, token: str, row: Row, url: str) -> bool:
        link_id = row["project_link_id"]
        try:
            data = _graphql(
                token, "query ($id: String!) { entityExternalLink(id: $id) { url label } }", {"id": link_id},
            )
            existing = data["entityExternalLink"]
        except LinearError:
            existing = None

        if existing:
            if existing == {"url": url, "label": "Таймер"}:
                return True
            data = _graphql(
                token,
                """
                mutation ($id: String!, $input: EntityExternalLinkUpdateInput!) {
                    entityExternalLinkUpdate(id: $id, input: $input) { success }
                }
                """,
                {"id": link_id, "input": {"label": "Таймер", "url": url}},
            )
            return data["entityExternalLinkUpdate"]["success"]
        data = _graphql(
            token,
            "mutation ($input: EntityExternalLinkCreateInput!) { entityExternalLinkCreate(input: $input) { success } }",
            {"input": {"id": link_id, "projectId": row["id"], "label": "Таймер", "url": url}},
        )
        return data["entityExternalLinkCreate"]["success"]
