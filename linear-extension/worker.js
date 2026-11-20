const defaultOrigin = "http://localhost:8765";
const panelPage = chrome.runtime.getURL("panel.html");

chrome.runtime.onMessage.addListener((request, sender, send) => {
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
    } else if ((request.action === "pause" || request.action === "resume") && validInterval && validVersion) {
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
    if (response.ok) return { ...messageBase, timerControls: true, status: response.status, data };
    const message = typeof data.detail === "string" ? data.detail : "Проверьте данные и повторите.";
    return { ...messageBase, status: response.status, message };
  } catch {
    return { ...messageBase, status: 503, message: "Трекер недоступен. Проверьте, что он запущен." };
  }
}
