import re
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tracker.app import create_app
from tracker.db import Database


def test_statistics_uses_complete_personal_history_and_local_periods(monkeypatch, tmp_path):
    path = tmp_path / "tracker.sqlite3"
    password = "local-statistics-test-password"
    for name, value in {
        "TRACKER_CONFIG": "targets.example.toml",
        "TRACKER_DB": str(path),
        "TRACKER_PASSWORD": password,
        "TRACKER_SECURE_COOKIE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_REDIRECT_URI", "TRACKER_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    database = Database(path)
    zone = ZoneInfo("Europe/Moscow")

    def ms(local):
        return int(datetime.fromisoformat(local).replace(tzinfo=zone).timestamp()) * 1000

    with TestClient(create_app()) as browser:
        with database.transaction(write=True) as conn:
            conn.execute(
                "INSERT INTO intervals (target, started_ms, ended_ms) VALUES (?, ?, ?)",
                ("issue-b", ms("2025-03-02T22:00"), ms("2025-03-02T23:00")),
            )
            conn.execute(
                "INSERT INTO intervals (target, started_ms, ended_ms) VALUES (?, ?, ?)",
                ("issue-a", ms("2025-03-02T23:50"), ms("2025-03-03T00:10")),
            )
            for minute in range(16):
                conn.execute(
                    "INSERT INTO intervals (target, started_ms, ended_ms, worked_ms) VALUES (?, ?, ?, ?)",
                    ("issue-a", ms(f"2025-03-03T10:{minute:02}"), ms(f"2025-03-03T12:{minute:02}"), 600_000),
                )
            conn.execute(
                "INSERT INTO intervals (target, started_ms) VALUES (?, ?)",
                ("issue-b", ms("2025-03-03T16:00")),
            )
        database.record_agent_run("issue-b", "agent", ms("2025-03-03T09:00"), ms("2025-03-03T10:00"))

        url = "/statistics?period=day&on=2025-03-03"
        login = browser.get(url)
        assert "Динамика фокуса" not in login.text
        action = unescape(re.search(r'action="([^"]+)"', login.text)[1])
        nonce = re.search(r'name="nonce" value="([^"]+)"', login.text)[1]
        result = browser.post(action, data={"credential": password, "nonce": nonce}, follow_redirects=False)
        assert result.headers["location"] == url

        day = browser.get(url)
        assert day.status_code == 200
        assert "<dd>3 ч 00 мин" in day.text
        assert "<dd>17" in day.text
        assert "+2 ч 00 мин" in day.text
        assert "00:00 · 20 мин" in day.text
        assert "Задача A" in day.text
        assert day.text.count('class="statistics-entry-name"') == 17
        assert "href=\"/t/project#active\"" in day.text

        week = browser.get("/statistics?period=week&on=2025-03-05")
        assert "3–9 марта 2025" in week.text
        assert "<dd>3 ч 00 мин" in week.text
        assert 'period=week&amp;on=2025-03-02' in week.text

        month = browser.get("/statistics?period=month&on=2025-03-05")
        assert "1–31 марта 2025" in month.text
        assert "<dd>4 ч 00 мин" in month.text
        assert month.text.count('class="statistics-entry-name"') == 18

        empty = browser.get("/statistics?period=day&on=2025-03-04")
        assert "За этот период нет завершённых сессий." in empty.text
