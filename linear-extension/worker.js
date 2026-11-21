const defaultOrigin = "http://localhost:8765";
const panelPage = chrome.runtime.getURL("panel.html");

chrome.runtime.onMessage.addListener((request, sender, send) => {
  if (request.action === "open-destination" && sender.id === chrome.runtime.id && sender.frameId === 0 && sender.tab) {
    openDestination(request.href, sender).then(send, () => send(false));
    return true;
  }
  if (sender.id !== chrome.runtime.id || sender.url?.split("?")[0] !== panelPage || !sender.tab?.url) {
    return false;
  }
  handle(request, sender.tab.url).then(send, () => send({
    status: 503, message: "Не удалось связаться с трекером.",
  }));
  return true;
});

async function handle(request, tabUrl) {
  const { trackerOrigin = defaultOrigin } = await chrome.storage.local.get("trackerOrigin");
  const tracker = new URL(trackerOrigin).origin;
  const messageBase = { tracker, projectIssues: true };
  const linearPage = new URL(tabUrl);
  if (linearPage.origin !== "https://linear.app" || linearPage.pathname !== request.path) {
    return { ...messageBase, status: 409, message: "Карточка Linear сменилась. Обновляем таймеры." };
  }

  const fields = new URLSearchParams({ url: linearPage.origin + linearPage.pathname });
  let path = "/inline/state?" + fields;
  const options = { credentials: "include", redirect: "error", signal: AbortSignal.timeout(12000) };
  if (request.action === "project-issues") {
    if (typeof request.after === "string" && request.after.length <= 500) fields.set("after", request.after);
    if (Number.isInteger(request.limit) && request.limit >= 1 && request.limit <= 250) {
      fields.set("limit", request.limit);
    }
    path = "/inline/project-issues?" + fields;
  } else if (request.action !== "state") {
    fields.set("csrf", request.csrf ?? "");
    const validInterval = Number.isSafeInteger(request.interval) && request.interval > 0;
    const validVersion = Number.isSafeInteger(request.version) && request.version >= 0;
    if (request.action === "stop" && validInterval) {
      path = "/inline/stop/" + request.interval;
      options.headers = { "X-Tracker-Completion": "1" };
    } else if (request.action === "complete" && validInterval) {
      path = "/linear/history/" + request.interval + "/complete";
    } else if (["accept", "discard"].includes(request.action) && validInterval) {
      options.headers = { "X-Tracker-Completion": "1" };
      path = "/inline/overtime/" + request.action + "/" + request.interval;
    } else if (["pause", "resume", "extend"].includes(request.action) && validInterval && validVersion) {
      path = "/inline/timers/" + request.action + "/" + request.interval;
      fields.set("version", request.version);
    } else if (request.action === "restart" && validInterval && /^[a-f0-9]{32}$/.test(request.key)) {
      path = "/inline/history/" + request.interval + "/restart";
      fields.set("key", request.key);
    } else if ((request.action === "start" || request.action === "start-project-issue")
      && /^[a-f0-9]{32}$/.test(request.key)) {
      const projectIssue = request.action === "start-project-issue";
      if (projectIssue ? typeof request.issueId !== "string" || !/^[a-f0-9-]{36}$/i.test(request.issueId)
        : request.issueId !== undefined) {
        return { ...messageBase, status: 400, message: "Не удалось определить задачу. Обновите вкладку Linear." };
      }
      path = "/inline/start";
      fields.set("key", request.key);
      if (request.minutes !== undefined) {
        if (!Number.isInteger(request.minutes) || request.minutes < 1 || request.minutes > 1440) {
          return { ...messageBase, status: 400, message: "Укажите длительность от 1 до 1440 минут." };
        }
        fields.set("pomo_minutes", request.minutes);
      }
      if (projectIssue) fields.set("issue_id", request.issueId);
    } else if (request.action === "agent" && /^[a-f0-9]{32}$/.test(request.key)) {
      path = "/inline/agent-runs";
      fields.set("key", request.key);
      fields.set("started", request.started ?? "");
      fields.set("ended", request.ended ?? "");
    } else {
      return { ...messageBase, status: 400, message: "Не удалось прочитать команду таймера." };
    }
    options.method = "POST";
    options.body = fields;
  }

  try {
    const response = await fetch(tracker + path, options);
    if (response.status === 204) return { ...messageBase, status: 204 };
    if (response.status === 401) return { ...messageBase, status: 401 };
    if (response.status === 404) {
      if (request.action === "project-issues") {
        return { ...messageBase, status: 404, message: "Проверьте адрес и версию сервиса трекера." };
      }
      return { ...messageBase, status: 404, message: "Откройте трекер и подключите аккаунт Linear." };
    }
    const data = await response.json();
    if (response.ok && ["stop", "accept", "discard"].includes(request.action)) return { ...messageBase, status: 204, finishedIssue: data.finishedIssue };
    if (response.ok) return { ...messageBase, timerControls: true, pomoControls: true, status: response.status, data };
    const message = typeof data.detail === "string" ? data.detail : "Проверьте данные и повторите.";
    return { ...messageBase, status: response.status, message };
  } catch {
    return { ...messageBase, status: 503, message: "Трекер недоступен. Проверьте, что он запущен." };
  }
}

const trackerScriptId = "tracker-project-links";

async function registerTrackerScript() {
  const { trackerOrigin = defaultOrigin } = await chrome.storage.local.get("trackerOrigin");
  const origin = new URL(trackerOrigin);
  const matches = [origin.protocol + "//" + origin.hostname + "/*"];
  const script = { id: trackerScriptId, matches, js: ["tracker-links.js"] };
  const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [trackerScriptId] });
  if (existing.length) await chrome.scripting.updateContentScripts([script]);
  else await chrome.scripting.registerContentScripts([script]);
}

chrome.runtime.onInstalled.addListener(registerTrackerScript);
chrome.runtime.onStartup.addListener(registerTrackerScript);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.trackerOrigin) registerTrackerScript();
});

async function openDestination(href, sender) {
  const { trackerOrigin = defaultOrigin } = await chrome.storage.local.get("trackerOrigin");
  if (sender.origin !== new URL(trackerOrigin).origin) return false;
  const destination = new URL(href);
  if (destination.origin !== "https://linear.app" || destination.username || destination.password ||
      !/^\/[\w.-]+\/(?:project\/[^/]+\/issues|team\/[^/]+\/all)$/.test(destination.pathname)) return false;
  const workspace = destination.pathname.split("/")[1];
  const linearTabs = await chrome.tabs.query({ url: "https://linear.app/*" });
  const tabs = linearTabs.filter(tab => {
    const path = new URL(tab.url).pathname;
    return path === "/" || path.split("/")[1] === workspace;
  });
  // Prefer an already-open destination, then a tab in the tracker's window.
  function rank(tab) {
    const path = new URL(tab.url).pathname;
    const match = path === destination.pathname ? 4 : path === "/" ? 0 : 2;
    return match + (tab.windowId === sender.tab.windowId ? 1 : 0);
  }
  tabs.sort((a, b) => rank(b) - rank(a) || (b.lastAccessed || 0) - (a.lastAccessed || 0));
  const tab = tabs[0];
  if (!tab) {
    await chrome.tabs.create({ windowId: sender.tab.windowId, url: destination.href });
  } else {
    const change = { active: true };
    if (tab.url !== destination.href) change.url = destination.href;
    await chrome.tabs.update(tab.id, change);
    if (tab.windowId !== sender.tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
  }
  return true;
}
