from fastapi.testclient import TestClient

from tracker.app import create_app


def test_health_and_private_tracker_boot(monkeypatch, tmp_path):
    for name, value in {
        "TRACKER_CONFIG": "targets.example.toml",
        "TRACKER_DB": str(tmp_path / "tracker.sqlite3"),
        "TRACKER_PASSWORD": "tracker-ci-health-test-password",
        "TRACKER_SECURE_COOKIE": "1",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("VERCEL", "TRACKER_DATABASE_URL", "TRACKER_TARGETS_TOML",
                 "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)

    with TestClient(create_app(), base_url="https://tracker.test") as client:
        health = client.get("/healthz")
        assert health.status_code == 204
        assert health.content == b""
        assert health.headers["cache-control"] == "no-store"

        home = client.get("/", follow_redirects=False)
        assert home.status_code == 303
        assert home.headers["location"] == "/t/project"
        assert client.get("/t/project/state").status_code == 401

        login = client.get("/t/project")
        assert login.status_code == 200
        assert 'name="credential"' in login.text
        assert "Secure" in login.headers["set-cookie"]
