const byId = id => document.getElementById(id);
const toolbarToggle = byId("toggle");
const toggleLabel = byId("toggle-label");
const toolbar = toolbarToggle.parentElement;
const quick = byId("quick");
const quickLabel = byId("quick-label");
const panel = byId("panel");
const panelContent = panel.querySelector(".panel-content");
const sections = byId("sections");
const agent = byId("agent");
const manual = byId("manual");
const status = byId("status");
const projectIssues = byId("project-issues");
const moreIssues = byId("more-issues");
let path = new URLSearchParams(location.search).get("path");
let state;
let syncedAt;
let fetching = false;
let changing = false;
let revision = 0;
let lastAction;
let errors = true;
let projectIssuesSupported = false;
let timerControlsSupported = false;
const lists = new Map();
const issuePageSize = 8;
let issuePage = newIssuePage();
const emptyMessages = {
  active: "Нет запущенных таймеров",
  personal: "Нет завершённых интервалов",
  agents: "Нет записей о запусках",
};

function newIssuePage() {
  return {
    items: new Map(), limit: issuePageSize, more: false,
    loaded: false, loading: false, queued: false, error: "",
  };
}

function duration(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map(value => String(value).padStart(2, "0")).join(":");
}

function tick() {
  if (!state) return;
  const now = state.now + performance.now() - syncedAt;
  const running = state.active.find(row => row.target === state.target.slug);
  const paused = running?.runningSince === null;
  const kind = state.target.kind === "project" ? "Проект" : "Задача";
  quick.toggleAttribute("data-running", Boolean(running));
  quick.toggleAttribute("data-paused", paused && timerControlsSupported);
  quickLabel.textContent = running
    ? kind + " · " + duration(timerElapsed(running, now))
    : (state.target.kind === "project" ? "По проекту" : "По задаче");
  const action = paused && timerControlsSupported ? "Продолжить" : running ? "Остановить" : "Запустить";
  quick.setAttribute("aria-label", action + " таймер «" + state.target.name + "»");
  for (const text of document.querySelectorAll("[data-started]")) {
    text.textContent = duration(Number(text.dataset.worked) + Math.max(0, now - Number(text.dataset.started)));
  }
}

function timerElapsed(row, now) {
  const since = row.runningSince ?? row.started;
  return (row.worked ?? 0) + (row.runningSince === null ? 0 : Math.max(0, now - since));
}

function showSection(id) {
  const section = byId(id);
  if (section instanceof HTMLDetailsElement) section.open = true;
  panel.scrollTop += section.getBoundingClientRect().top - panel.getBoundingClientRect().top - 8;
}

function reportSize() {
  const expanded = !panel.hidden;
  const height = toolbar.offsetHeight + Math.ceil(panelContent.getBoundingClientRect().height) + 3;
  window.parent.postMessage({ type: "linear-timer-size", expanded, height }, "https://linear.app");
}

function show(expanded) {
  panel.hidden = !expanded;
  toolbarToggle.setAttribute("aria-expanded", String(expanded));
  reportSize();
  if (expanded) loadIssues(true);
}

function sourceName(name, href) {
  const legacy = typeof href === "string" && /^\/linear\/source\/[0-9a-f-]{36}$/.test(href);
  const link = legacy ? new URL(href, state.trackerOrigin).href
    : href?.startsWith("https://linear.app/") ? href : null;
  const element = document.createElement(link ? "a" : "span");
  element.textContent = name;
  if (link) {
    element.href = link;
    element.target = "_blank";
    element.rel = "noopener noreferrer";
    element.className = "linear-source";
    element.title = "Открыть в Linear";
  }
  return element;
}

function renderIssueRows() {
  if (projectIssues.hidden || !state) return;
  const active = new Map(state.active.map(row => [row.target, row]));
  const rows = [...issuePage.items.values()];
  const signature = JSON.stringify(rows.map(issue => [issue, active.get("linear-" + issue.id)]));
  if (lists.get("issues") === signature) return;
  lists.set("issues", signature);
  const list = byId("issue-rows");
  list.replaceChildren();
  for (const issue of rows) {
    const entry = document.createElement("div");
    entry.className = "entry";
    const info = document.createElement("div");
    info.className = "entry-info";
    const name = sourceName(issue.title, issue.url);
    const identifier = document.createElement("p");
    identifier.className = "subtle";
    identifier.textContent = issue.identifier;
    info.append(name, identifier);
    const timer = active.get("linear-" + issue.id);
    const paused = timer?.runningSince === null && timerControlsSupported;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "issue-action";
    button.textContent = paused ? "Продолжить" : timer ? "Остановить" : "Запустить";
    button.setAttribute("aria-label", button.textContent + " таймер задачи «" + issue.title + "»");
    button.addEventListener("click", event => {
      if (!event.isTrusted) return;
      if (paused) change("resume", { interval: timer.id, version: timer.version });
      else if (timer) change("stop", { interval: timer.id });
      else change("start-project-issue", { issueId: issue.id });
    });
    entry.append(info, button);
    list.append(entry);
  }
}

function showIssueProgress() {
  const notice = byId("issue-status");
  byId("issue-rows").hidden = !projectIssuesSupported;
  if (!projectIssuesSupported) {
    notice.textContent = "Chromium использует старую версию расширения. Нажмите ↻ на его карточке в chrome://extensions и обновите вкладку Linear.";
    notice.hidden = false;
    moreIssues.hidden = true;
    return;
  }
  notice.textContent = issuePage.loading && !issuePage.loaded ? "Загружаем задачи…"
    : issuePage.error || (issuePage.loaded && !issuePage.items.size ? "В проекте пока нет задач." : "");
  notice.hidden = !notice.textContent;
  moreIssues.hidden = !issuePage.more && !issuePage.error;
  moreIssues.disabled = issuePage.loading;
  moreIssues.textContent = issuePage.error ? "Повторить" : "Ещё задачи";
}

async function loadIssues(queueIfBusy = false) {
  if (document.hidden || panel.hidden || state?.target.kind !== "project" || !projectIssuesSupported) return;
  const page = issuePage;
  if (page.loading) {
    page.queued ||= queueIfBusy;
    return;
  }
  const currentPath = path;
  const limit = page.limit;
  const items = new Map();
  let after = null;
  let reply;
  page.loading = true;
  showIssueProgress();
  do {
    reply = await send("project-issues", { after, limit: Math.min(limit - items.size, 250) });
    if (currentPath !== path || issuePage !== page) return;
    if (reply.status !== 200) break;
    for (const issue of reply.data.nodes) items.set(issue.id, issue);
    const next = reply.data.pageInfo.hasNextPage ? reply.data.pageInfo.endCursor : null;
    after = next !== after ? next : null;
  } while (after && items.size < limit && !panel.hidden && !document.hidden);
  page.loading = false;
  if (reply.status === 401) {
    showError(reply);
    return;
  }
  if (reply.status === 200) {
    page.items = items;
    page.loaded = true;
    page.more = Boolean(after);
    page.error = "";
    renderIssueRows();
  } else {
    page.error = reply.message || "Не удалось загрузить задачи проекта.";
  }
  showIssueProgress();
  if (page.queued) {
    page.queued = false;
    loadIssues();
  }
}

function timerButton(className, label, row, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.setAttribute("aria-label", label + " «" + row.name + "»");
  button.addEventListener("click", event => {
    if (event.isTrusted) handler();
  });
  return button;
}

function renderList(rows, id, timezone) {
  const open = id === "personal" ? new Set(state.active.map(row => row.target)) : new Set();
  const serialized = JSON.stringify([rows, timezone, timerControlsSupported, [...open]]);
  if (lists.get(id) === serialized) return;
  lists.set(id, serialized);
  const list = byId(id);
  const focusId = list.contains(document.activeElement) ? document.activeElement.id : "";
  list.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = emptyMessages[id];
    list.append(empty);
    return;
  }
  const dateTime = new Intl.DateTimeFormat("ru", {
    timeZone: timezone, dateStyle: "short", timeStyle: "medium",
  });
  for (const row of rows) {
    const entry = document.createElement("div");
    entry.className = "entry";
    const paused = row.runningSince === null;
    entry.toggleAttribute("data-paused", paused);
    const info = document.createElement("div");
    info.className = "entry-info";
    const name = sourceName(row.name, row.linearUrl);
    const times = document.createElement("p");
    times.className = "subtle";
    times.textContent = row.ended === null
      ? (paused ? "На паузе · " : "") + "С " + dateTime.format(row.started)
      : dateTime.formatRange(row.started, row.ended);
    info.append(name, times);
    const actions = document.createElement("div");
    actions.className = "entry-actions";
    const clock = document.createElement("span");
    clock.className = "duration";
    actions.append(clock);
    if (row.ended === null) {
      if (paused) clock.textContent = duration(row.worked);
      else {
        clock.dataset.started = row.runningSince ?? row.started;
        clock.dataset.worked = row.worked ?? 0;
      }
      if (timerControlsSupported) {
        const action = paused ? "resume" : "pause";
        const button = timerButton("playback " + action, (paused ? "Продолжить" : "Приостановить") + " таймер", row,
          () => change(action, { interval: row.id, version: row.version }));
        button.id = "playback-" + row.id;
        actions.append(button);
      }
      const stop = timerButton("stop", "Остановить таймер", row, () => change("stop", { interval: row.id }));
      stop.id = "stop-" + row.id;
      actions.append(stop);
    } else {
      clock.textContent = duration(row.worked ?? (row.ended - row.started));
      if (id === "personal" && timerControlsSupported) {
        const alreadyOpen = open.has(row.target);
        const restart = timerButton("history-action", alreadyOpen ? "К таймеру" : "Запустить снова", row,
          () => alreadyOpen ? showSection("timer-overview") : change("restart", { interval: row.id }));
        restart.textContent = alreadyOpen ? "К таймеру" : "Запустить снова";
        actions.append(restart);
      }
    }
    entry.append(info, actions);
    list.append(entry);
  }
  if (focusId) byId(focusId)?.focus({ preventScroll: true });
}

function render(data, capabilities) {
  state = { ...data, trackerOrigin: capabilities.tracker };
  projectIssuesSupported = capabilities.projectIssues === true;
  timerControlsSupported = capabilities.timerControls === true && data.timerControls === true;
  syncedAt = performance.now();
  const project = data.target.kind === "project";
  const toggleText = project ? "Таймеры и задачи" : "Таймеры";
  toggleLabel.textContent = toggleText + (data.active.length ? " · " + data.active.length : "");
  byId("target-kind").textContent = project ? "Проект" : "Задача";
  byId("target-kind").hidden = false;
  byId("target").replaceChildren(sourceName(data.target.name, data.target.linear_url || "https://linear.app" + path));
  byId("target").hidden = false;
  byId("setup").hidden = true;
  sections.hidden = false;
  manual.hidden = data.target.kind !== "issue";
  projectIssues.hidden = !project;
  document.querySelector('[data-section="project-issues"]').hidden = !project;
  byId("zone").textContent = "Часовой пояс · " + data.timezone;
  quick.hidden = false;
  const paused = data.active.filter(row => row.runningSince === null).length;
  byId("timer-counts").textContent = data.active.length
    ? `Идут ${data.active.length - paused}${paused ? ` · На паузе ${paused}` : ""}` : "";
  renderList(data.active, "active", data.timezone);
  renderList(data.personal, "personal", data.timezone);
  renderList(data.agents, "agents", data.timezone);
  renderIssueRows();
  if (project) showIssueProgress();
  if (!issuePage.loaded && !issuePage.error) loadIssues();
  tick();
}

function showError(reply, actionFailed = false) {
  errors = !actionFailed;
  status.textContent = reply.status === 401 ? "Войдите в трекер в этом браузере." : reply.message;
  if (reply.status === 401) {
    state = undefined;
    quick.hidden = sections.hidden = byId("target").hidden = true;
    byId("target-kind").hidden = true;
    toggleLabel.textContent = "Таймеры";
    lists.clear();
    issuePage = newIssuePage();
    for (const id of ["active", "personal", "agents", "issue-rows"]) byId(id).replaceChildren();
  }
  const setup = byId("setup");
  setup.hidden = !reply.tracker || ![401, 404, 502].includes(reply.status);
  if (!setup.hidden) {
    byId("setup-link").textContent = reply.status === 401 ? "Войти в трекер" : "Подключение Linear";
    byId("setup-link").href = reply.tracker + (reply.status === 401 ? "/" : "/linear");
  }
  if (!actionFailed) {
    quick.hidden = true;
    if (!state) {
      sections.hidden = true;
      byId("target").hidden = true;
      byId("target-kind").hidden = true;
    }
  }
  if (actionFailed) show(true);
}

async function send(action, fields = {}) {
  try {
    const reply = await chrome.runtime.sendMessage({ action, path, ...fields });
    if (reply) return reply;
  } catch {
    // A reloaded extension invalidates previously injected frames.
  }
  return { status: 503, message: "Обновите страницу Linear после перезагрузки расширения." };
}

async function refresh() {
  if (document.hidden || fetching || changing) return;
  fetching = true;
  const before = path;
  const previousRevision = revision;
  try {
    const reply = await send("state");
    if (before !== path || previousRevision !== revision) return;
    if (reply.status === 200) {
      if (errors) status.textContent = "";
      errors = false;
      render(reply.data, reply);
    } else {
      showError(reply);
    }
  } finally {
    fetching = false;
    if (before !== path || previousRevision !== revision) refresh();
  }
}

async function change(action, fields = {}) {
  if (!state || changing) return;
  changing = true;
  revision += 1;
  const before = path;
  const signature = JSON.stringify([path, action, fields]);
  if (lastAction?.signature !== signature) {
    lastAction = { signature, key: crypto.randomUUID().replaceAll("-", "") };
  }
  quick.disabled = true;
  try {
    const reply = await send(action, { ...fields, csrf: state.csrf, key: lastAction.key });
    if (before !== path) return;
    if (reply.status !== 204) {
      showError(reply, true);
      return;
    }
    lastAction = undefined;
    errors = false;
    status.textContent = action === "agent" ? "Запуск агента сохранён." : "";
    if (action === "agent") {
      agent.reset();
      manual.open = false;
      byId("journal").open = true;
    }
  } finally {
    quick.disabled = false;
    changing = false;
    if (before !== path) refresh();
  }
  refresh();
  if (action === "restart") showSection("timer-overview");
}

toolbarToggle.addEventListener("click", () => {
  show(panel.hidden);
  refresh();
});
moreIssues.addEventListener("click", () => {
  if (issuePage.more && !issuePage.error) issuePage.limit += issuePageSize;
  loadIssues(true);
});
for (const link of document.querySelectorAll("[data-section]")) {
  link.addEventListener("click", () => showSection(link.dataset.section));
}
quick.addEventListener("click", event => {
  if (!event.isTrusted || !state) return;
  const running = state.active.find(row => row.target === state.target.slug);
  if (running?.runningSince === null && timerControlsSupported) {
    change("resume", { interval: running.id, version: running.version });
  } else if (running) change("stop", { interval: running.id });
  else change("start");
});
agent.addEventListener("submit", event => {
  event.preventDefault();
  if (event.isTrusted) change("agent", Object.fromEntries(new FormData(agent)));
});
window.addEventListener("message", event => {
  if (event.source !== window.parent || event.origin !== "https://linear.app") return;
  if (event.data?.type === "linear-timer-issues-changed" && event.data.path === path) {
    loadIssues(true);
    return;
  }
  if (event.data?.type !== "linear-timer-path" || typeof event.data.path !== "string"
    || event.data.path === path) return;
  path = event.data.path;
  state = undefined;
  quick.hidden = true;
  sections.hidden = true;
  byId("target").hidden = true;
  byId("target-kind").hidden = true;
  byId("setup").hidden = true;
  status.textContent = "Загружаем таймеры…";
  errors = true;
  toggleLabel.textContent = "Таймеры";
  issuePage = newIssuePage();
  lists.delete("issues");
  byId("issue-rows").replaceChildren();
  agent.reset();
  manual.open = false;
  refresh();
});
function refreshVisible() {
  refresh();
  loadIssues();
}

document.addEventListener("visibilitychange", refreshVisible);
new ResizeObserver(reportSize).observe(panelContent);
window.addEventListener("focus", refreshVisible);
window.setInterval(refreshVisible, 5000);
window.setInterval(tick, 1000);
refresh();
