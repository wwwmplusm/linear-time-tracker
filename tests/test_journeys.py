import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "independent-browser-sessions-test"


class TrackerServer:
    def __init__(self, tmp_path):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.database = tmp_path / "tracker.sqlite3"
        self.env = {
            **os.environ,
            "TRACKER_CONFIG": str(ROOT / "targets.example.toml"),
            "TRACKER_DB": str(self.database),
            "TRACKER_PASSWORD": PASSWORD,
            "TRACKER_SECURE_COOKIE": "0",
        }

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tracker.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT,
            env=self.env,
        )
        deadline = time.monotonic() + 10
        while self.process.poll() is None and time.monotonic() < deadline:
            try:
                with urlopen(self.base_url + "/t/issue-a", timeout=0.25):
                    return
            except (URLError, TimeoutError):
                time.sleep(0.05)
        self.stop()
        pytest.fail("Uvicorn did not start")

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=10)

    def rows(self, sql):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute(sql).fetchall()


@pytest.fixture
def server(tmp_path):
    instance = TrackerServer(tmp_path)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        executable = os.environ.get("TRACKER_TEST_CHROMIUM") or shutil.which("chromium")
        browser = playwright.chromium.launch(executable_path=executable)
        yield browser
        browser.close()


@pytest.fixture
def browsers(chromium, server):
    laptop = chromium.new_context(base_url=server.base_url)
    phone = chromium.new_context(
        base_url=server.base_url, viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True,
    )
    try:
        yield laptop.new_page(), phone.new_page()
    finally:
        laptop.close()
        phone.close()


def sign_in(page, slug):
    page.goto(f"/t/{slug}")
    expect(page.get_by_role("button", name="Войти")).to_be_visible()
    page.get_by_label("Пароль владельца").fill(PASSWORD)
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("button", name="Выйти")).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_text(
        {"project": "Учебный проект", "issue-a": "Задача A", "issue-b": "Задача B"}[slug]
    )


def active_row(page, name):
    return page.locator(".active-entry").filter(has=page.get_by_role("link", name=name, exact=True))


def test_parallel_timers_sync_and_survive_restart(server, browsers):
    laptop, phone = browsers
    sign_in(laptop, "issue-a")
    sign_in(phone, "issue-b")
    assert phone.context.cookies()[0]["value"] != laptop.context.cookies()[0]["value"]

    laptop.get_by_role("button", name="Запустить", exact=True).click()
    laptop.get_by_role("navigation").get_by_role("link", name="Задача B").click()
    laptop.get_by_role("button", name="Запустить", exact=True).click()
    expected_start = active_row(laptop, "Задача B").locator("time").get_attribute("datetime")
    expect(phone.locator(".active-entry")).to_have_count(2, timeout=6500)
    expect(active_row(phone, "Задача B").locator("time")).to_have_attribute("datetime", expected_start)
    assert phone.evaluate("document.documentElement.scrollWidth === innerWidth")

    phone.get_by_role("button", name="Остановить таймер «Задача A»", exact=True).click()
    expect(active_row(phone, "Задача A")).to_have_count(0)
    expect(phone.locator("#personal-journal .entry-main").get_by_role("link")).to_have_text("Задача A")
    with laptop.expect_response(lambda res: res.url.endswith("/t/issue-b/state")):
        laptop.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(laptop.locator(".active-entry")).to_have_count(1)

    phone.reload()
    expect(active_row(phone, "Задача B").locator("time")).to_have_attribute("datetime", expected_start)
    server.stop()
    server.start()
    phone.reload()
    expect(active_row(phone, "Задача B").locator("time")).to_have_attribute("datetime", expected_start)
    expect(phone.locator(".active-entry")).to_have_count(1)

    phone.get_by_role("button", name="Выйти").click()
    expect(phone.get_by_role("button", name="Войти")).to_be_visible()
    laptop.reload()
    expect(active_row(laptop, "Задача B")).to_have_count(1)


def test_concurrent_start_and_retry_of_old_actions(server, browsers):
    laptop, phone = browsers
    sign_in(laptop, "issue-a")
    sign_in(phone, "issue-a")
    first_form = laptop.locator('form[action$="/start"]')
    first_body = first_form.evaluate("(form) => Object.fromEntries(new FormData(form))")
    def is_start(response):
        return response.url.endswith("/t/issue-a/start") and response.request.method == "POST"

    with laptop.expect_response(is_start) as laptop_start, phone.expect_response(is_start) as phone_start:
        start_at = int(time.time() * 1000) + 750
        for page in (laptop, phone):
            page.locator('form[action$="/start"]').evaluate(
                """(form, at) => {
                    const body = new URLSearchParams(new FormData(form));
                    setTimeout(() => {
                        window.startSent = Date.now();
                        fetch(form.action, { method: 'POST', body });
                    }, Math.max(0, at - Date.now()));
                }""",
                start_at,
            )
    assert laptop_start.value.status == phone_start.value.status == 303
    assert abs(laptop.evaluate("window.startSent") - phone.evaluate("window.startSent")) < 250

    rows = server.rows("SELECT id, started_ms, ended_ms FROM intervals")
    assert len(rows) == 1 and rows[0][2] is None
    old_id, old_start, _ = rows[0]
    assert server.rows("SELECT interval_id FROM starts") == [(old_id,), (old_id,)]

    laptop.reload()
    old_form = laptop.locator(".active-entry form[action*='/stop/']")
    old_stop = old_form.get_attribute("action")
    old_body = old_form.evaluate("(form) => Object.fromEntries(new FormData(form))")
    old_form.get_by_role("button").click()
    old_end = server.rows("SELECT ended_ms FROM intervals")[0][0]
    assert old_end >= old_start

    old_start_url = "/t/issue-a/start"
    assert laptop.request.post(old_start_url, form=first_body).ok
    assert server.rows("SELECT id FROM intervals") == [(old_id,)]

    laptop.get_by_role("button", name="Запустить", exact=True).click()
    assert phone.request.post(old_stop, form=old_body).status == 403
    assert laptop.request.post(old_stop, form=old_body).ok
    assert laptop.request.post(old_start_url, form=first_body).ok
    rows = server.rows("SELECT id, started_ms, ended_ms FROM intervals ORDER BY id")
    assert rows[0] == (old_id, old_start, old_end)
    new_id, new_start, new_end = rows[1]
    assert new_id != old_id and new_start >= old_end and new_end is None
    phone.reload()
    expect(phone.locator(".active-entry form[action*='/stop/']")).to_have_attribute("action", f"/t/issue-a/stop/{new_id}")


def test_pause_and_restart_from_history(server, browsers):
    laptop, phone = browsers
    sign_in(laptop, "issue-a")
    sign_in(phone, "issue-b")
    laptop.get_by_role("button", name="Запустить", exact=True).click()
    laptop.get_by_role("button", name="Приостановить таймер «Задача A»").click()

    paused = active_row(phone, "Задача A")
    expect(paused.get_by_role("button", name="Продолжить таймер «Задача A»")).to_be_visible(timeout=6500)
    expect(phone.locator("#timer-indicator-text")).to_have_text("Идут 0 · На паузе 1")
    frozen = paused.locator(".duration").inner_text()
    paused.locator(".duration").evaluate(
        "(clock) => new Promise(resolve => setTimeout(() => resolve(clock.textContent), 1100))"
    )
    expect(paused.locator(".duration")).to_have_text(frozen)
    assert phone.evaluate("document.documentElement.scrollWidth === innerWidth")

    phone.get_by_role("button", name="Продолжить таймер «Задача A»").click()
    expect(laptop.get_by_role("button", name="Приостановить таймер «Задача A»")).to_be_visible(timeout=6500)
    phone.get_by_role("button", name="Остановить таймер «Задача A»", exact=True).click()
    history = phone.locator("#personal-journal")
    expect(history.locator("li")).to_have_count(1)
    history.get_by_role("button", name="Запустить снова «Задача A»").click()
    expect(active_row(phone, "Задача A")).to_be_visible()
    expect(history.locator("li")).to_have_count(1)
    assert server.rows("SELECT target, ended_ms IS NULL FROM intervals ORDER BY id") == [("issue-a", 0), ("issue-a", 1)]


def test_overlapping_agent_runs_have_individual_durations(server, browsers):
    laptop, phone = browsers
    sign_in(laptop, "issue-a")
    sign_in(phone, "issue-a")
    for page, start, end in (
        (laptop, "2026-11-20T12:00", "2026-11-20T13:15"),
        (phone, "2026-11-20T12:30", "2026-11-20T13:15"),
    ):
        page.get_by_text("Записать запуск агента").click()
        page.get_by_label("Начало").fill(start)
        page.get_by_label("Окончание").fill(end)
        page.get_by_role("button", name="Сохранить запуск").click()
        expect(page).to_have_url(server.base_url + "/t/issue-a")

    laptop.reload()
    durations = laptop.locator("#agents-journal .duration")
    expect(durations).to_have_text(["00:45:00", "01:15:00"])
    expect(laptop.locator("#agents-journal time").first).to_have_attribute(
        "datetime", "2026-11-20T09:30:00.000+00:00"
    )
    expect(laptop.locator("#personal-journal li")).to_have_count(0)
    assert server.rows("SELECT count(DISTINCT id) FROM agent_runs WHERE target = 'issue-a'") == [(2,)]

def test_pomodoro_overtime_and_edit(server, browsers):
    laptop, phone = browsers
    sign_in(laptop, "issue-a")
    sign_in(phone, "issue-b")
    laptop.get_by_role("button", name="Помодоро для «Задача A»").click()
    dialog = laptop.get_by_role("dialog", name="Помодоро")
    dialog.get_by_label("Длительность, мин").fill("60")
    dialog.get_by_role("button", name="Начать помодоро").click()
    row = active_row(laptop, "Задача A")
    expect(row).to_contain_text("Помодоро · 60 мин")
    row.get_by_role("button", name="Продлить помодоро на 5 минут").click()
    expect(row).to_contain_text("Помодоро · 65 мин")

    # Move the start into the past to simulate returning after a long absence.
    with sqlite3.connect(server.database) as conn:
        conn.execute("UPDATE intervals SET started_ms = started_ms - 4200000")
    phone.reload()
    overtime = active_row(phone, "Задача A")
    expect(overtime).to_contain_text("65 мин в журнале")
    expect(phone.locator("#personal-journal .duration")).to_have_text("01:05:00")
    overtime.get_by_role("button", name="Засчитать").click()
    expect(overtime).to_have_count(0)
    assert server.rows("SELECT count(*) FROM intervals") == [(1,)]
    worked, = server.rows("SELECT worked_ms FROM intervals")[0]
    assert 4200000 <= worked < 4230000

    phone.locator("#personal-journal").get_by_role("link", name="Редактировать").click()
    end = phone.get_by_label("Окончание")
    end.fill("2025-01-01T10:10:00.123")
    phone.get_by_role("button", name="Сохранить", exact=True).click()
    expect(phone.get_by_role("alert")).to_contain_text("Окончание должно быть позже начала")
    phone.get_by_label("Начало").fill("2025-01-01T09:10:00.123")
    phone.get_by_role("button", name="Сохранить", exact=True).click()
    expect(phone.locator("#personal-journal .duration")).to_have_text("01:00:00")
    assert server.rows("SELECT count(*), worked_ms, started_ms % 1000 FROM intervals") == [(1, 3600000, 123)]
