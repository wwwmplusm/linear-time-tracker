import re
from datetime import UTC, datetime, timedelta
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


class CalendarCatalog:
    def __init__(self, linear):
        self.linear = linear
        self.lock = Lock()
        self.items = {}
        self.checked = self.full = float("-inf")
        self.since = None
        self.token = None

    def load(self):
        token = self.linear.access_token()
        with self.lock:
            now = monotonic()
            if token == self.token and now - self.checked < 8:
                return self.items
            full = token != self.token or now - self.full > 300
            items = {} if full else self.items.copy()
            since = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            filters = {"state": {"type": {"nin": ["completed", "canceled"]}}} if full else {
                "updatedAt": {"gte": self.since}}
            after = None
            while True:
                page = _graphql(token, """
                    query TrackerCalendarIssues($filter: IssueFilter!, $after: String) {
                      issues(first: 50, filter: $filter, after: $after, orderBy: updatedAt) {
                        nodes { id identifier title description priority team { key } project { id name }
                                state { type } labels(first: 20) { nodes { name } } }
                        pageInfo { hasNextPage endCursor }
                      }
                    }
                """, {"filter": filters, "after": after})["issues"]
                for issue in page["nodes"]:
                    slug = "linear-" + issue["id"]
                    if issue["state"]["type"] in {"completed", "canceled"}:
                        items.pop(slug, None)
                        continue
                    items[slug] = describe(issue["title"], team=issue["team"]["key"],
                        project=(issue["project"] or {}).get("name", ""), priority=issue["priority"],
                        description=issue["description"], identifier=issue["identifier"],
                        labels=[label["name"] for label in issue["labels"]["nodes"]])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            after = None
            while True:
                page = _graphql(token, """
                    query TrackerCalendarProjects($after: String, $filter: ProjectFilter) {
                      projects(first: 50, after: $after, filter: $filter) {
                        nodes { id name description priority teams(first: 10) { nodes { key } } }
                        pageInfo { hasNextPage endCursor }
                      }
                    }
                """, {"after": after, "filter": None if full else {"updatedAt": {"gte": self.since}}})["projects"]
                for project in page["nodes"]:
                    keys = [team["key"] for team in project["teams"]["nodes"]]
                    key = next((key for key in keys if key in WORK), next(iter(keys), ""))
                    items["linear-" + project["id"]] = dict(
                        describe(project["name"], team=key, project=project["name"],
                                 description=project["description"], priority=project["priority"] or 0),
                        kind="project")
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            self.items, self.token, self.since, self.checked = items, token, since, now
            if full:
                self.full = now
            return items
