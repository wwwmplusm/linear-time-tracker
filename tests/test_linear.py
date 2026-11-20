import hashlib
import json
import re
import sqlite3
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from tracker import linear
from tracker.app import create_app


BASE_URL = "http://localhost:8765"
TRACKER_ORIGIN = "https://tracker.example.com"
PASSWORD = "independent-owner-password-for-test"
TEAM = "9cfb482a-81e3-4154-b5b9-2c805e70a02d"
PROJECT = "40f13d2f-d913-4cbe-b6bb-902f62732205"
ISSUE = "5c79e91e-1c4d-4fb5-9d54-41c08a542921"

UNASSIGNED = "0f712331-672e-4c09-b778-bddc1471bc69"


def field(response, name):
    return re.search(rf'name="{name}" value="([^"]+)"', response.text)[1]


def sign_in(browser):
    login = browser.get("/t/project")
    browser.post("/t/project/login", data={"credential": PASSWORD, "nonce": field(login, "nonce")})


def test_linear_oauth_browse_start_and_link_publication(monkeypatch, tmp_path):
    for name, value in {
        "TRACKER_CONFIG": "targets.example.toml",
        "TRACKER_DB": str(tmp_path / "tracker.sqlite3"),
        "TRACKER_PASSWORD": PASSWORD,
        "TRACKER_SECURE_COOKIE": "0",
        "LINEAR_CLIENT_ID": "test-client",
        "LINEAR_CLIENT_SECRET": "test-secret",
        "LINEAR_REDIRECT_URI": BASE_URL + "/oauth/callback",
        "LINEAR_TRACKER_ORIGIN": TRACKER_ORIGIN,
    }.items():
        monkeypatch.setenv(name, value)

    clock = linear.now_ms()
    monkeypatch.setattr(linear, "now_ms", lambda: clock)
    exchanges = []
    catalog_queries = []
    projects = {}
    attachments = {}

    def result_page(nodes, *, next_cursor=None, previous_cursor=None):
        return {
            "nodes": nodes,
            "pageInfo": {
                "hasNextPage": bool(next_cursor), "endCursor": next_cursor,
                "hasPreviousPage": bool(previous_cursor), "startCursor": previous_cursor,
            },
        }

    def simulated_linear(url, payload, headers):
        if url == linear.TOKEN_URL:
            fields = {key: values[0] for key, values in parse_qs(payload.decode()).items()}
            assert fields["client_id"] == "test-client"
            assert fields["client_secret"] == "test-secret"
            exchanges.append(fields)
            if fields["grant_type"] == "authorization_code":
                return {"access_token": "initial-access", "refresh_token": "initial-refresh", "expires_in": 5}
            assert fields["refresh_token"] == "initial-refresh"
            return {"access_token": "next-access", "refresh_token": "next-refresh", "expires_in": 3600}

        assert url == linear.GRAPHQL_URL
        data = json.loads(payload)
        query, variables = data["query"], data["variables"]
        if "viewer" in query:
            assert headers["Authorization"] == "Bearer initial-access"
            return {"data": {"viewer": {"name": "Владелец"}}}
        assert headers["Authorization"] == "Bearer next-access"
        if "query Tracker" in query:
            catalog_queries.append((query, variables))
        if "TrackerTeams" in query:
            return {"data": {"organization": {"urlKey": "demo"}, "page": result_page([{"id": TEAM, "name": "Команда разработки", "key": "DEV"}])}}
        if "TrackerProjects" in query:
            assert variables["team"] == TEAM
            return {"data": {"organization": {"urlKey": "demo"}, "team": {
                "name": "Команда разработки", "key": "DEV", "page": result_page([{"id": PROJECT, "name": "Разработка", "url": "https://linear.app/demo/project/dev"}]),
            }}}
        if "TrackerUnassigned" in query:
            assert variables["team"] == TEAM and variables["filter"]["project"] == {"null": True}
            return {"data": {"organization": {"urlKey": "demo"}, "team": {
                "name": "Команда разработки", "key": "DEV",
                "page": result_page([{"id": UNASSIGNED, "name": "Задача без проекта", "identifier": "DEV-2", "url": "https://linear.app/demo/issue/DEV-2"}]),
            }}}
        if "TrackerIssues" in query:
            assert variables["team"] == TEAM and variables["project"] == PROJECT
            assert variables["membership"] == variables["filter"]["team"] == {"id": {"eq": TEAM}}
            term = variables["filter"].get("title", {}).get("containsIgnoreCase", "")
            issue = {"id": ISSUE, "name": "Первая задача", "identifier": "DEV-1", "url": "https://linear.app/demo/issue/DEV-1"}
            if variables["after"]:
                assert variables["after"] == "next/page"
                issue_page = result_page([], previous_cursor="previous/page")
            else:
                assert not variables["before"] or variables["before"] == "previous/page"
                nodes = [issue] if term.casefold() in issue["name"].casefold() else []
                issue_page = result_page(nodes, next_cursor="next/page" if not term else None)
            return {"data": {
                "team": {"name": "Команда разработки"},
                "project": {"name": "Разработка", "url": "https://linear.app/demo/project/dev", "teams": {"nodes": [{"id": TEAM}]}, "page": issue_page},
            }}
        if "project(id:" in query:
            assert variables["id"] == PROJECT
            return {"data": {"project": {"id": PROJECT, "name": "Разработка", "url": "https://linear.app/demo/project/dev"}}}
        if "issue(id:" in query:
            issue_id = variables["id"]
            assert issue_id in {ISSUE, UNASSIGNED}
            return {"data": {"issue": {
                "id": issue_id, "identifier": "DEV-1" if issue_id == ISSUE else "DEV-2",
                "url": "https://linear.app/demo/issue/" + ("DEV-1" if issue_id == ISSUE else "DEV-2"),
                "title": "Первая задача" if issue_id == ISSUE else "Задача без проекта",
                "team": {"id": TEAM}, "project": {"id": PROJECT} if issue_id == ISSUE else None,
            }}}
        if "entityExternalLink(id:" in query:
            link = projects.get(variables["id"])
            if not link:
                return {"errors": [{"message": "missing project link"}]}
            return {"data": {"entityExternalLink": {"url": link["url"], "label": link["label"]}}}
        record = variables["input"]
        if "entityExternalLinkCreate" in query:
            assert record["id"] not in projects
            projects[record["id"]] = record
            return {"data": {"entityExternalLinkCreate": {"success": True}}}
        if "attachmentCreate" in query:
            attachments[record["issueId"], record["url"]] = record
            return {"data": {"attachmentCreate": {"success": True}}}
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(linear, "_post", simulated_linear)
    app = create_app()
    with TestClient(app, base_url=BASE_URL) as browser:
        login = browser.get("/")
        assert login.url.path == "/linear" and 'action="/linear/login"' in login.text
        home = browser.post("/linear/login", data={"credential": PASSWORD, "nonce": field(login, "nonce")})
        assert home.url.path == "/linear" and "Подключить Linear" in home.text
        begin = browser.get("/linear/authorize", follow_redirects=False)
        authorization = urlsplit(begin.headers["location"])
        assert (authorization.hostname, authorization.path) == ("linear.app", "/oauth/authorize")
        parameters = {key: values[0] for key, values in parse_qs(authorization.query).items()}
        assert parameters["redirect_uri"] == BASE_URL + "/oauth/callback"
        assert parameters["code_challenge_method"] == "S256"
        callback = f"/oauth/callback?state={parameters['state']}&code=test-code"
        other = TestClient(app, base_url=BASE_URL)
        sign_in(other)
        assert other.get(callback).url.path == "/linear"
        assert not exchanges

        assert "Владелец" in browser.get(callback).text
        assert exchanges[0]["code"] == "test-code"
        verifier = exchanges[0]["code_verifier"].encode()
        expected = urlsafe_b64encode(hashlib.sha256(verifier).digest()).decode().rstrip("=")
        assert parameters["code_challenge"] == expected
        assert "устарел" in browser.get(callback).text
        assert [exchange["grant_type"] for exchange in exchanges] == ["authorization_code", "refresh_token"]

        teams = browser.get("/linear")
        assert "Команды Linear" in teams.text and f'href="/linear?team={TEAM}"' in teams.text
        assert "Первая задача" not in teams.text
        projects_page = browser.get("/linear", params={"team": TEAM})
        assert f'href="/linear?team={TEAM}&amp;project={PROJECT}"' in projects_page.text
        assert "Первая задача" not in projects_page.text

        project_page = f"/linear?team={TEAM}&project={PROJECT}"
        start_url = project_page.replace("/linear?", "/linear/targets/start?")
        listed_issues = browser.get(project_page)
        sidebar, workspace = listed_issues.text.split('<main class="workspace">', 1)
        assert 'href="https://linear.app/demo/team/DEV/all"' in sidebar
        assert 'href="https://linear.app/demo/issue/DEV-1"' in workspace
        assert 'href="https://linear.app/demo/project/dev"' in workspace
        assert "Первая задача" in workspace and "Без проекта" not in workspace
        assert "Без проекта" in sidebar and "Разработка" in sidebar
        calls = len(catalog_queries)
        browser.get(project_page)
        assert len(catalog_queries) == calls
        rev = re.search(r'data-revision="([^"]+)"', listed_issues.text)[1]
        assert browser.get("/linear/state", headers={"X-Timer-Revision": rev}).status_code == 204
        assert "after=next%2Fpage" in listed_issues.text
        assert "before=previous%2Fpage" in browser.get(project_page + "&after=next%2Fpage").text
        assert "Совпадений нет" in browser.get(project_page + "&q=missing").text

        for kind, name, entity in [
            ("project", "Разработка", PROJECT),
            ("issue", "Первая задача", ISSUE),
        ]:
            found = browser.get(project_page)
            assert f"Запустить таймер для «{name}»" in found.text
            csrf = field(found, "csrf")
            form = {"kind": kind, "reference": entity, "csrf": csrf, "key": field(found, "key")}
            assert browser.post(start_url, data={**form, "csrf": "invalid"}).status_code == 403
            assert browser.get(f"/t/linear-{entity}").status_code == 404
            started = browser.post(start_url, data=form)
            assert started.status_code == 200 and started.request.url.query == found.request.url.query
            assert f"Открыть активный таймер для «{name}»" in started.text
            assert "Таймер идёт" in browser.get(f"/t/linear-{entity}").text
            repeated = browser.post(start_url, data=form)
            assert repeated.text.count(f'data-active-target="linear-{entity}"') == 1
            interval = re.search(rf'data-active-target="linear-{entity}".*?/linear/stop/(\d+)', repeated.text, re.S)[1]
            stop_url = project_page.replace("/linear?", f"/linear/stop/{interval}?")
            stopped = browser.post(stop_url, data={"csrf": csrf})
            assert stopped.url.query == found.url.query
            assert f'data-active-target="linear-{entity}"' not in stopped.text
            assert f'data-active-target="linear-{entity}"' not in browser.post(start_url, data=form).text
            assert name in browser.get(f"/t/linear-{entity}").text.split('id="personal-journal"')[1]
            link_url = f"/linear/targets/{entity}/link"
            assert browser.post(link_url, data={"csrf": "invalid"}).status_code == 403
            for _ in range(2):
                page = browser.post(link_url, data={"csrf": csrf})
                assert "сохранена в Linear" in page.text

        wrong_project = browser.post(start_url, data={**form, "reference": UNASSIGNED})
        assert wrong_project.status_code == 422 and "выбранном разделе" in wrong_project.text
        assert browser.get(f"/t/linear-{UNASSIGNED}").status_code == 404
        unassigned = browser.get(f"/linear?team={TEAM}&project=none")
        assert "Задача без проекта" in unassigned.text and "Первая задача" not in unassigned.text.split("<details")[0]
        unassigned_start = browser.post(
            f"/linear/targets/start?team={TEAM}&project=none",
            data={"kind": "issue", "reference": UNASSIGNED, "csrf": csrf, "key": field(unassigned, "key")},
        )
        assert f'data-active-target="linear-{UNASSIGNED}"' in unassigned_start.text

        assert list(projects.values())[0]["url"] == f"{TRACKER_ORIGIN}/t/linear-{PROJECT}"
        assert list(projects.values())[0]["label"] == "Таймер"
        assert list(attachments) == [(ISSUE, f"{TRACKER_ORIGIN}/t/linear-{ISSUE}")]
        assert exchanges[1]["grant_type"] == "refresh_token"
        assert len(exchanges) == 2

    # Simulate targets imported by an older tracker, before source URLs were stored.
    with sqlite3.connect(tmp_path / "tracker.sqlite3") as db:
        db.execute("ALTER TABLE linear_targets DROP COLUMN linear_url")
    with TestClient(create_app(), base_url=BASE_URL) as restarted:
        sign_in(restarted)
        old_link = restarted.get(f"/linear/source/{ISSUE}", follow_redirects=False)
        assert old_link.headers["location"] == "https://linear.app/demo/issue/DEV-1"
        assert 'href="https://linear.app/demo/issue/DEV-1"' in restarted.get(f"/t/linear-{ISSUE}").text
        assert "Владелец" in restarted.get("/linear").text
        assert "Первая задача" in restarted.get(f"/t/linear-{ISSUE}").text
