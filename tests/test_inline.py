import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tracker.app import create_app
from tracker.config import Target
from tracker.linear import LinearIntegration


ROOT = Path(__file__).resolve().parents[1]
OWNER_PASSWORD = "local-embedded-panel-test-password"
LINEAR_ISSUE = "/demo/issue/DEMO-1/test-issue"
LINEAR_PROJECT = "/demo/project/test-project/issues"
TARGETS = {
    ("issue", "DEMO-1"): Target("linear-11111111-1111-4111-8111-111111111111", "Тестовая задача", "issue", "https://linear.app/demo/issue/DEMO-1"),
    ("project", "test-project"): Target("linear-22222222-2222-4222-8222-222222222222", "Тестовый проект", "project", "https://linear.app/demo/project/test-project"),
}


def sign_in(page, url):
    page.goto(url)
    page.get_by_label("Пароль владельца").fill(OWNER_PASSWORD)
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("button", name="Выйти")).to_be_visible()


@pytest.fixture
def live_issue_rows():
    issue = TARGETS["issue", "DEMO-1"]
    return [{"id": issue.slug.removeprefix("linear-"), "title": issue.name, "identifier": "DEMO-1", "url": issue.linear_url}]


@pytest.fixture
def inline_server(monkeypatch, tmp_path, live_issue_rows):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://localhost:{listener.getsockname()[1]}"
        for name, value in {
            "TRACKER_CONFIG": str(ROOT / "targets.example.toml"),
            "TRACKER_DB": str(tmp_path / "tracker.sqlite3"),
            "TRACKER_PASSWORD": OWNER_PASSWORD,
            "TRACKER_SECURE_COOKIE": "0",
            "LINEAR_CLIENT_ID": "test-client",
            "LINEAR_CLIENT_SECRET": "test-secret",
            "LINEAR_REDIRECT_URI": origin + "/oauth/callback",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(LinearIntegration, "workspace", lambda self: "demo")
        issue = TARGETS["issue", "DEMO-1"]
        issue_id = issue.slug.removeprefix("linear-")
        project_id = TARGETS["project", "test-project"].slug.removeprefix("linear-")

        def resolve(self, kind, ref, *, project_id=None):
            if project_id:
                assert ref == issue_id
                assert project_id == TARGETS["project", "test-project"].slug.removeprefix("linear-")
                return issue, "DEMO-1"
            return TARGETS[kind, ref], ref

        def issues(self, id, after, *, limit=8):
            assert id == project_id and after is None
            return {
                "nodes": live_issue_rows[:limit],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }

        monkeypatch.setattr(LinearIntegration, "resolve_target", resolve)
        monkeypatch.setattr(LinearIntegration, "project_issues", issues)

        config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            yield origin
        finally:
            server.should_exit = True
            thread.join(timeout=8)


@pytest.mark.parametrize("stale_worker", [False, True], ids=["current", "stale-worker"])
def test_track_directly_inside_linear(tmp_path, inline_server, stale_worker, live_issue_rows):
    origin = inline_server
    extension = ROOT / "linear-extension"
    if stale_worker:
        extension = shutil.copytree(extension, tmp_path / "linear-extension")
        worker_file = extension / "worker.js"
        worker_file.write_text(worker_file.read_text().replace("{ tracker, projectIssues: true }", "{ tracker }"))
        manifest_file = extension / "manifest.json"
        stale_manifest = json.loads(manifest_file.read_text()) | {"version": "1.0.0"}
        manifest_file.write_text(json.dumps(stale_manifest))
    executable = os.environ.get("TRACKER_TEST_CHROMIUM") or shutil.which("chromium")
    browser_options = {"executable_path": executable} if executable else {"channel": "chromium"}
    with sync_playwright() as playwright:
        profile = playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, **browser_options,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )
        worker = profile.service_workers[0] if profile.service_workers else profile.wait_for_event("serviceworker")
        worker.evaluate("(origin) => chrome.storage.local.set({trackerOrigin: origin})", origin)
        issue_page = profile.new_page()
        sign_in(issue_page, origin)
        issue_page.route(
            "https://linear.app/**",
            lambda route: route.fulfill(
                body="<html><h1>Test Linear page</h1></html>",
                headers={"content-security-policy": "default-src 'self'; frame-src *; style-src 'unsafe-inline'"},
                content_type="text/html",
            ),
        )
        panel = issue_page.frame_locator('iframe[title="Личный таймер для Linear"]')
        if stale_worker:
            issue_page.goto("https://linear.app" + LINEAR_PROJECT)
            panel.get_by_role("button", name="Таймеры и задачи").click()
            expect(panel.get_by_text("Нажмите ↻ на его карточке в chrome://extensions", exact=False)).to_be_visible()
            expect(panel.locator("#issue-rows button")).to_have_count(0)
            profile.close()
            return

        issue_page.goto("https://linear.app" + LINEAR_ISSUE)
        expect(panel.locator("#quick")).to_have_text("По задаче")
        panel.get_by_role("button", name="Запустить таймер «Тестовая задача»").click()
        expect(panel.get_by_role("button", name="Остановить таймер «Тестовая задача»", exact=True)).to_be_visible()

        issue_page.evaluate("(path) => history.pushState({}, '', path)", LINEAR_PROJECT)
        expect(panel.locator("#quick")).to_have_text("По проекту")
        panel.get_by_role("button", name="Запустить таймер «Тестовый проект»").click()
        native_issue = live_issue_rows.pop()
        panel.get_by_role("button", name="Таймеры и задачи · 2").click()
        expect(panel.get_by_role("heading", name="Тестовый проект")).to_be_visible()
        expect(panel.get_by_text("Записать запуск агента")).to_be_hidden()
        expect(panel.locator("#active .entry")).to_have_count(2)
        expect(panel.locator("#active a").filter(has_text="Тестовая задача")).to_have_attribute("href", TARGETS["issue", "DEMO-1"].linear_url)
        expect(panel.get_by_text("В проекте пока нет задач.")).to_be_visible()
        live_issue_rows.append(native_issue)
        issue_page.evaluate("""() => {
            const row = document.createElement('div');
            row.id = 'native-issue-row';
            row.setAttribute('role', 'row');
            const identifier = document.createElement('span');
            identifier.textContent = 'DEMO-1';
            row.append(identifier);
            document.body.append(row);
        }""")
        row_timer = panel.locator("#issue-rows").get_by_text("DEMO-1", exact=True)
        expect(row_timer).to_be_visible(timeout=1800)
        expect(panel.locator("#issue-rows a").first).to_have_attribute("href", native_issue["url"])

        second_browser = playwright.chromium.launch(**browser_options)
        tracker = second_browser.new_page()
        sign_in(tracker, origin)
        panel.locator("#active").get_by_role("button", name="Приостановить таймер «Тестовая задача»").click()
        expect(tracker.get_by_role("button", name="Продолжить таймер «Тестовая задача»")).to_be_visible(timeout=7000)
        tracker.get_by_role("button", name="Продолжить таймер «Тестовая задача»").click()
        expect(panel.locator("#active").get_by_role("button", name="Приостановить таймер «Тестовая задача»")).to_be_visible(timeout=7000)
        tracker.get_by_role("button", name="Остановить таймер «Тестовая задача»", exact=True).click()
        expect(panel.locator("#active .entry")).to_have_count(1, timeout=7500)
        panel.get_by_role("button", name="Запустить таймер задачи «Тестовая задача»").click()
        expect(panel.locator("#active .entry")).to_have_count(2)
        assert issue_page.url.endswith(LINEAR_PROJECT)
        panel.get_by_role("button", name="Остановить таймер задачи «Тестовая задача»").click()
        expect(panel.locator("#active .entry")).to_have_count(1)
        panel.locator("#quick").click()
        expect(panel.locator("#active .entry")).to_have_count(0)
        second_browser.close()

        live_issue_rows.clear()
        issue_page.locator("#native-issue-row").evaluate("(row) => row.remove()")
        expect(panel.get_by_text("В проекте пока нет задач.")).to_be_visible(timeout=1800)
        live_issue_rows.append(native_issue)
        expect(row_timer).to_be_visible(timeout=6500)

        issue_page.evaluate("(path) => history.pushState({}, '', path)", LINEAR_ISSUE)
        expect(panel.get_by_role("heading", name="Тестовая задача")).to_be_visible()
        panel.get_by_text("Записать запуск агента").click()
        panel.get_by_label("Начало").fill("2026-11-20T12:00")
        panel.get_by_label("Окончание").fill("2026-11-20T12:45")
        panel.get_by_role("button", name="Сохранить запуск").click()
        expect(panel.get_by_text("Запуск агента сохранён.")).to_be_visible()
        expect(panel.locator("#agents .duration")).to_have_text("00:45:00")
        expect(panel.locator("#personal .entry")).to_have_count(3)
        panel.locator("#personal").get_by_role("button", name="Запустить снова «Тестовый проект»").click()
        expect(panel.locator("#active .entry")).to_have_count(1)
        expect(panel.locator("#active .entry")).to_contain_text("Тестовый проект")
        profile.close()
