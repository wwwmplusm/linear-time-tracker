import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Target:
    slug: str
    name: str
    kind: str

    @property
    def url(self) -> str:
        return f"/t/{self.slug}"


@dataclass(frozen=True)
class Settings:
    database: Path
    password: str
    secure_cookie: bool
    timezone: ZoneInfo
    targets: dict[str, Target]

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.environ.get("TRACKER_PASSWORD", "")
        if len(password) < 24:
            raise ValueError("TRACKER_PASSWORD must contain at least 24 characters")
        secure_cookie = os.environ.get("TRACKER_SECURE_COOKIE", "1")
        if secure_cookie not in {"0", "1"}:
            raise ValueError("TRACKER_SECURE_COOKIE must be 0 or 1")

        path = Path(os.environ.get("TRACKER_CONFIG", "targets.toml"))
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
        )
