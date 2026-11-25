import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from threading import Lock
from time import monotonic

from tracker.linear import _graphql


PRIORITIES = ("Без приоритета", "Срочно", "Высокий", "Средний", "Низкий")
WORK = {"TUT", "RAN", "PER", "HUS"}
STUDY = {"HSE"}


def describe(name, *, team="", project="", labels=(), priority=0, description="", identifier=""):
    context = " ".join((name, project, *labels)).casefold()
    if team in WORK:
        area = "work"
    elif team in STUDY:
        area = "study"
    elif re.search(r"трениров|здоров|спорт|колено|плеч[ои]|врач|медицин|бассейн|пробеж|фитнес|стоматолог", context):
        area = "health"
    elif re.search(r"чтени|почитать|читать|прочитать|книг", context):
        area = "reading"
    elif re.search(r"близки|семь[яеёи]|родител|друзья|друзей|друзьям|с друзьями|свидани|душевн|молитв|с мамой|с папой", context):
        area = "social"
    elif re.search(r"репетитор|лицей|лицее|стажиров|хасл|\bперо\b|урок с", context):
        area = "work"
    elif re.search(r"уч[её]б|изуч|экзамен|зач[её]т|универ|китайский|диплом|лекци", context):
        area = "study"
    else:
        area = "other"
    lesson = next((v for v in labels if re.search(r"урок|занятие|лекция|семинар", v, re.I)), "")
    if not lesson and (team == "TUT" or re.search(r"урок|занятие|лекция|семинар", context)):
        lesson = project or "Уроки и занятия"
    return dict(name=name, area=area, project=project, lesson=lesson, priority=PRIORITIES[priority],
                summary=" ".join((description or "").split())[:180], identifier=identifier)


@lru_cache(maxsize=4096)
def local_description(name, team, kind):
    item = describe(name, team=team, project=name if kind == "project" else "")
    return {key: value for key, value in item.items() if value or key == "name"}


CATALOG_QUERY = """
    query TrackerCalendar($issues: IssueFilter!, $projects: ProjectFilter,
                          $issueCursor: String, $projectCursor: String,
                          $readIssues: Boolean!, $readProjects: Boolean!) {
      issues(first: 50, filter: $issues, after: $issueCursor, orderBy: updatedAt) @include(if: $readIssues) {
        nodes { id identifier title description priority team { key } project { name }
                state { type } labels(first: 20) { nodes { name } } }
        pageInfo { hasNextPage endCursor }
      }
      projects(first: 50, filter: $projects, after: $projectCursor) @include(if: $readProjects) {
        nodes { id name description priority teams(first: 10) { nodes { key } } }
        pageInfo { hasNextPage endCursor }
      }
    }
"""


class CalendarCatalog:
    def __init__(self, linear):
        self.linear = linear
        self.lock = Lock()
        self.snapshot = None
        self.checked = self.full = float("-inf")
        self.since = None
        self.token = None

    def load(self):
        token = self.linear.access_token()
        with self.lock:
            now = monotonic()
            if token == self.token and now - self.checked < 8:
                return self.snapshot
            full = token != self.token or now - self.full > 300
            items = {} if full else self.snapshot[0].copy()
            since = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            filters = {"state": {"type": {"nin": ["completed", "canceled"]}}} if full else {
                "updatedAt": {"gte": self.since}}
            variables = dict(issues=filters, projects=None if full else {"updatedAt": {"gte": self.since}},
                             readIssues=True, readProjects=True, issueCursor=None, projectCursor=None)
            while variables["readIssues"] or variables["readProjects"]:
                data = _graphql(token, CATALOG_QUERY, variables)
                for issue in data.get("issues", {}).get("nodes", ()):
                    slug = "linear-" + issue["id"]
                    if issue["state"]["type"] in {"completed", "canceled"}:
                        items.pop(slug, None)
                        continue
                    items[slug] = describe(issue["title"], team=issue["team"]["key"],
                        project=(issue["project"] or {}).get("name", ""), priority=issue["priority"],
                        description=issue["description"], identifier=issue["identifier"],
                        labels=[label["name"] for label in issue["labels"]["nodes"]])
                for project in data.get("projects", {}).get("nodes", ()):
                    keys = [team["key"] for team in project["teams"]["nodes"]]
                    key = next((key for key in keys if key in WORK), next(iter(keys), ""))
                    items["linear-" + project["id"]] = dict(
                        describe(project["name"], team=key, project=project["name"],
                                 description=project["description"], priority=project["priority"] or 0),
                        kind="project")
                for field, flag, cursor in (("issues", "readIssues", "issueCursor"),
                                            ("projects", "readProjects", "projectCursor")):
                    if variables[flag]:
                        info = data[field]["pageInfo"]
                        variables[flag] = info["hasNextPage"]
                        variables[cursor] = info["endCursor"]
            if self.snapshot is None or items != self.snapshot[0]:
                body = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                self.snapshot = (items, body, etag)
            self.token, self.since, self.checked = token, since, now
            if full:
                self.full = now
            return self.snapshot
