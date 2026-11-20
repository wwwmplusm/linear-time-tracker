import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Target:
    slug: str
    name: str
    kind: str
    linear_url: str | None = None

    def __post_init__(self):
        if self.linear_url and urlsplit(self.linear_url)[:2] != ("https", "linear.app"):
            raise ValueError("Expected an HTTPS link to linear.app")

    @property
    def source_href(self) -> str | None:
        if self.linear_url:
            return self.linear_url
        if re.fullmatch(r"linear-[0-9a-f-]{36}", self.slug):
            return "/linear/source/" + self.slug[7:]
        return None

    @property
    def url(self) -> str:
        return f"/t/{self.slug}"


@dataclass(frozen=True)
class Settings:
    database: Path
    password: str = field(repr=False)
    secure_cookie: bool
    timezone: ZoneInfo
    targets: dict[str, Target]
    database_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        vercel = os.environ.get("VERCEL") == "1"
        database_url = os.environ.get("TRACKER_DATABASE_URL")
        if vercel:
            database_url = database_url or os.environ.get("DATABASE_URL")
            if not database_url:
                raise ValueError("Configure a persistent PostgreSQL database for Vercel")
        if database_url and urlsplit(database_url).scheme not in {"postgres", "postgresql"}:
            raise ValueError("The tracker database URL must use PostgreSQL")

        password = os.environ.get("TRACKER_PASSWORD", "")
        if len(password) < 24:
            raise ValueError("TRACKER_PASSWORD must contain at least 24 characters")
        secure_cookie = os.environ.get("TRACKER_SECURE_COOKIE", "1")
        if secure_cookie not in {"0", "1"}:
            raise ValueError("TRACKER_SECURE_COOKIE must be 0 or 1")
        if vercel and secure_cookie != "1":
            raise ValueError("Vercel requires secure tracker cookies")

        config_text = os.environ.get("TRACKER_TARGETS_TOML")
        if config_text is not None:
            config = tomllib.loads(config_text)
        else:
            default_path = Path("targets.toml")
            if vercel:
                default_path = Path(__file__).resolve().parent.parent / "targets.example.toml"
            path = Path(os.environ.get("TRACKER_CONFIG", default_path))
            with path.open("rb") as file:
                config = tomllib.load(file)
        targets = {}
        for entry in config["targets"]:
            target = Target(**entry)
            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target.slug)
                or target.slug in targets
                or not target.name.strip()
                or target.kind not in {"project", "issue"}
            ):
                raise ValueError(f"Invalid or repeated tracker target: {target.slug}")
            targets[target.slug] = target
        if not targets:
            raise ValueError("Configure at least one tracker target")
        return cls(
            database=Path(os.environ.get("TRACKER_DB", "data/tracker.sqlite3")),
            password=password,
            secure_cookie=secure_cookie == "1",
            timezone=ZoneInfo(config.get("timezone", "Europe/Moscow")),
            targets=targets,
            database_url=database_url,
        )
