import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tracker.app import create_app


PASSWORD = "local-calendar-test-password"
WEEK_URL = "/calendar?week=2026-01-01"


@pytest.fixture
def browser(monkeypatch, tmp_path):
    for name, value in {
        "TRACKER_CONFIG": "targets.example.toml",
        "TRACKER_DB": str(tmp_path / "tracker.sqlite3"),
        "TRACKER_PASSWORD": PASSWORD,
        "TRACKER_SECURE_COOKIE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_REDIRECT_URI",
        "TRACKER_DATABASE_URL", "TRACKER_TARGETS_TOML",
    ):
        monkeypatch.delenv(name, raising=False)

    with TestClient(create_app()) as client:
        yield client


def sign_in(browser, url=WEEK_URL):
    login = browser.get(url)
    assert login.template.name == "login.html"
    assert login.context["login_url"] == url.replace("/calendar", "/calendar/login")
    nonce = login.context["nonce"]
    response = browser.post(
        login.context["login_url"], data={"credential": PASSWORD, "nonce": nonce}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == url
    page = browser.get(url)
    assert page.status_code == 200
    assert page.template.name == "calendar.html"
    assert 'action="/logout"' in page.text
    return page.context


def test_calendar_week_and_login(browser):
    page = sign_in(browser)
    assert page["days"] == [
        "2025-12-29", "2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
    ]
    assert page["previous_week"] == "2025-12-22"
    assert page["next_week"] == "2026-01-05"
    assert page["blocks"] == []
    assert page["targets"]["issue-a"].name == "Задача A"
    assert re.fullmatch(r"[0-9a-f]{64}", page["csrf"])
    current = browser.get("/calendar").context
    assert current["today"] == datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    assert current["today"] in current["days"]
    for day in ("20260101", "2026-W01-1", "2026-02-30", "0001-01-01", "9999-12-31"):
        assert browser.get("/calendar", params={"week": day}).status_code == 422


def test_calendar_crud_and_conflicts(browser):
    block_id = str(uuid4())
    endpoint = f"/calendar/blocks/{block_id}"
    delete_endpoint = endpoint + "/delete"
    block = {"target": "issue-a", "day": "2026-01-04", "duration": 30}
    csrf = sign_in(browser)["csrf"]
    form = {**block, "csrf": csrf, "version": 0}

    assert browser.post(endpoint, data={**form, "version": 1}).status_code == 409
    created = browser.post(endpoint, data=form)
    assert created.status_code == 204
    assert created.content == b""
    assert browser.post(endpoint, data=form).status_code == 409
    assert browser.get(WEEK_URL).context["blocks"] == [
        {**block, "id": block_id, "start_minute": None, "version": 1},
    ]

    moved = {"target": "issue-b", "day": "2026-01-05", "start_minute": 1410, "duration": 30}
    assert browser.post(endpoint, data={**moved, "csrf": csrf, "version": 1}).status_code == 204
    assert browser.post(endpoint, data={**form, "version": 1}).status_code == 409
    assert browser.post(delete_endpoint, data={"csrf": csrf, "version": 1}).status_code == 409
    assert browser.get(WEEK_URL).context["blocks"] == []
    following = "/calendar?week=2026-01-05"
    assert browser.get(following).context["blocks"] == [{**moved, "id": block_id, "version": 2}]

    assert browser.post(delete_endpoint, data={"csrf": csrf, "version": 2}).status_code == 204
    assert browser.post(delete_endpoint, data={"csrf": csrf, "version": 2}).status_code == 409
    assert browser.post(endpoint, data=form).status_code == 409
    assert browser.get(following).context["blocks"] == []


def test_calendar_auth_csrf_and_validation(browser):
    endpoint = f"/calendar/blocks/{uuid4()}"
    delete_endpoint = endpoint + "/delete"
    form = {"target": "issue-a", "day": "2026-01-01", "start_minute": 0, "duration": 60, "version": 0}
    assert browser.post(endpoint, data={**form, "csrf": "invalid"}).status_code == 401
    assert browser.post(delete_endpoint, data={"csrf": "invalid", "version": 1}).status_code == 401
    csrf = sign_in(browser)["csrf"]
    assert browser.post(endpoint, data=form).status_code == 422
    assert browser.post(endpoint, data={**form, "csrf": "invalid"}).status_code == 403
    assert browser.post(delete_endpoint, data={"version": 1}).status_code == 422
    assert browser.post(delete_endpoint, data={"csrf": "invalid", "version": 1}).status_code == 403

    valid = {**form, "csrf": csrf}
    for change in (
        {"day": "2026-02-29"}, {"day": "20260101"}, {"start_minute": -1},
        {"start_minute": 1440}, {"start_minute": 1439}, {"duration": 0}, {"duration": 1441},
        {"version": -1}, {"version": 2**63},
    ):
        assert browser.post(endpoint, data={**valid, **change}).status_code == 422
    assert browser.post(endpoint, data={**valid, "target": "missing"}).status_code == 404
    assert browser.post("/calendar/blocks/invalid", data=valid).status_code == 422
    assert browser.post(delete_endpoint, data={"csrf": csrf, "version": 0}).status_code == 422
    assert browser.get(WEEK_URL).context["blocks"] == []
    assert browser.post(endpoint, data=valid).status_code == 204
    assert browser.get(WEEK_URL).context["blocks"][0]["start_minute"] == 0


def test_calendar_concurrent_updates_have_one_winner(browser):
    endpoint = f"/calendar/blocks/{uuid4()}"
    csrf = sign_in(browser)["csrf"]
    form = {"target": "issue-a", "day": "2026-01-01", "duration": 30, "csrf": csrf}
    assert browser.post(endpoint, data={**form, "version": 0}).status_code == 204

    def update(duration):
        return browser.post(endpoint, data={**form, "version": 1, "duration": duration}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (45, 60)))

    assert sorted(results) == [204, 409]
    block, = browser.get(WEEK_URL).context["blocks"]
    assert block["version"] == 2
    assert block["duration"] == (45, 60)[results.index(204)]


def test_calendar_areas_and_description():
    from tracker.calendar_catalog import describe
    for team in ("TUT", "RAN", "PER", "HUS"):
        assert describe("Задача", team=team)["area"] == "work"
    assert describe("Прочитать учебник", team="HSE")["area"] == "study"
    assert describe("Документы", team="LIFE")["area"] == "other"
    assert describe("Упражнения на колено", team="LIFE")["area"] == "health"
    assert describe("Вечер с близкими", team="LIFE")["area"] == "social"
    assert describe("Почитать роман", team="LIFE")["area"] == "reading"
    lesson = describe("Урок с Машей", team="TUT", project="Маша", priority=2, description="Разобрать времена")
    assert (lesson["lesson"], lesson["priority"], lesson["summary"]) == ("Маша", "Высокий", "Разобрать времена")
