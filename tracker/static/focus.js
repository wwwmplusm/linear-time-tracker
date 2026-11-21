(() => {
  const dialog = document.getElementById("focus-dialog");
  const input = document.getElementById("focus-query");
  const results = document.getElementById("focus-results");
  const status = document.getElementById("focus-status");
  let controller;
  let delay;
  let saving = false;
  const csrf = document.querySelector('.header-actions input[name="csrf"]').value;

  const issueSearch = document.getElementById("focus-issues");
  const destinations = document.getElementById("focus-destinations");
  const heading = document.getElementById("focus-title");
  const searchTitle = heading.textContent;
  const back = document.getElementById("destination-back");
  const list = document.getElementById("destination-list");
  const notice = document.getElementById("destination-status");
  const more = document.getElementById("destination-more");
  let directoryRequest;
  let team;
  let cursor;

  function destinationLink(name, href, kind) {
    const url = new URL(href);
    const parts = url.pathname.split("/");
    if (url.origin !== "https://linear.app" || parts[2] !== kind || !parts[1] || !parts[3]) {
      throw new Error("Invalid Linear destination");
    }
    if (kind === "project") url.pathname = parts.slice(0, 4).join("/") + "/issues";
    const link = document.createElement("a");
    link.href = url.href;
    link.dataset.linearDestination = "";
    link.textContent = name;
    return link;
  }

  async function loadDirectory(append = false) {
    directoryRequest?.abort();
    const request = new AbortController();
    directoryRequest = request;
    if (!append) {
      cursor = null;
      list.replaceChildren();
    }
    more.hidden = true;
    notice.textContent = team ? "Загружаем проекты…" : "Загружаем команды…";
    const params = new URLSearchParams();
    if (team) params.set("team", team.id);
    if (cursor) params.set("after", cursor);
    try {
      const response = await fetch("/linear/focus/destinations?" + params, { signal: request.signal });
      if (!response.ok) throw new Error("Directory unavailable");
      const page = await response.json();
      if (request.signal.aborted) return;
      const choices = [];
      if (team && !append) choices.push(destinationLink("Все задачи команды", page.teamUrl, "team"));
      for (const item of page.items) {
        if (team) {
          choices.push(destinationLink(item.name, item.url, "project"));
        } else {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = item.name;
          button.addEventListener("click", () => showDirectory(item));
          choices.push(button);
        }
      }
      for (const choice of choices) {
        const li = document.createElement("li");
        li.append(choice);
        list.append(li);
      }
      notice.textContent = !append && !page.items.length
        ? (team ? "В этой команде пока нет проектов." : "Команды не найдены.") : "";
      cursor = page.next;
      more.textContent = "Показать ещё";
      more.hidden = !cursor;
    } catch {
      if (request.signal.aborted) return;
      notice.textContent = "Не удалось загрузить список из Linear.";
      more.textContent = "Повторить";
      more.hidden = false;
    }
  }

  function showDirectory(selectedTeam) {
    clearTimeout(delay);
    controller?.abort();
    team = selectedTeam;
    heading.textContent = team ? "Проекты · " + team.name : "Выберите команду";
    back.textContent = team ? "← К командам" : "← К поиску задач";
    issueSearch.hidden = true;
    destinations.hidden = false;
    back.focus();
    loadDirectory();
  }

  function showIssueSearch() {
    directoryRequest?.abort();
    destinations.hidden = true;
    issueSearch.hidden = false;
    heading.textContent = searchTitle;
    input.focus();
    queueSearch(0);
  }

  document.getElementById("focus-browse").addEventListener("click", () => showDirectory(null));
  more.addEventListener("click", () => loadDirectory(Boolean(cursor)));
  back.addEventListener("click", () => team ? showDirectory(null) : showIssueSearch());

  async function search() {
    const request = new AbortController();
    controller = request;
    try {
      const term = input.value.trim();
      const response = await fetch("/linear/focus/search?" + new URLSearchParams({ q: term }), { signal: request.signal });
      if (!response.ok) throw new Error("Search failed");
      const issues = await response.json();
      if (request.signal.aborted) return;
      status.textContent = issues.length ? "" : "Задачи не найдены";
      const rows = issues.map(issue => {
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.issue = issue.id;
        const title = document.createElement("span");
        title.textContent = issue.title;
        const context = document.createElement("span");
        context.className = "muted";
        context.textContent = [issue.identifier, issue.team?.name, issue.project?.name].filter(Boolean).join(" · ");
        button.append(title, context);
        row.append(button);
        return row;
      });
      results.replaceChildren(...rows);
    } catch {
      if (!request.signal.aborted) status.textContent = "Не удалось загрузить задачи из Linear. Попробуйте ещё раз.";
    }
  }

  function queueSearch(wait) {
    clearTimeout(delay);
    controller?.abort();
    results.replaceChildren();
    status.textContent = "Поиск…";
    delay = setTimeout(search, wait);
  }

  document.addEventListener("click", event => {
    if (!event.target.closest("[data-open-focus]")) return;
    input.value = "";
    dialog.showModal();
    showIssueSearch();
  });
  input.addEventListener("input", () => queueSearch(180));
  dialog.addEventListener("close", () => {
    directoryRequest?.abort();
    clearTimeout(delay);
    controller?.abort();
  });
  dialog.addEventListener("keydown", event => {
    const buttons = [...results.querySelectorAll("button")];
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    if (index < 0 && document.activeElement !== input) return;
    if (event.key === "Enter" && document.activeElement === input) {
      event.preventDefault();
      buttons[0].click();
    } else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const next = index + (event.key === "ArrowDown" ? 1 : -1);
      (buttons[next] || input).focus();
    }
  });
  dialog.addEventListener("cancel", event => {
    if (saving) event.preventDefault();
  });
  results.addEventListener("click", async event => {
    const button = event.target.closest("button[data-issue]");
    if (!button || saving) return;
    saving = true;
    input.disabled = true;
    status.textContent = "Добавляем в фокус…";
    try {
      const response = await fetch("/linear/focus", {
        method: "POST", body: new URLSearchParams({ csrf, reference: button.dataset.issue }),
      });
      if (!response.ok) throw new Error("Save failed");
      const { url } = await response.json();
      if (dialog.hasAttribute("data-stay-on-dashboard")) location.reload();
      else location.assign(url);
    } catch {
      status.textContent = "Не удалось добавить задачу в фокус. Попробуйте ещё раз.";
    } finally {
      saving = false;
      input.disabled = false;
    }
  });
  const prompt = document.querySelector("[data-completion-prompt]");
  if (prompt) {
    const url = new URL(location.href);
    url.searchParams.delete("finished");
    history.replaceState(history.state, "", url);
    prompt.querySelector("[data-dismiss]").addEventListener("click", () => prompt.remove());
    const complete = prompt.querySelector("[data-complete-interval]");
    const message = prompt.querySelector('[role="status"]');
    const saved = message.textContent;
    complete.addEventListener("click", async () => {
      complete.disabled = true;
      try {
        const response = await fetch("/linear/history/" + complete.dataset.completeInterval + "/complete", {
          method: "POST", body: new URLSearchParams({ csrf }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Проверьте подключение к Linear.");
        message.textContent = saved + " · " + result.state;
        complete.hidden = true;
      } catch (error) {
        message.textContent = saved + ". Проверьте статус в Linear или повторите попытку. " +
          (error instanceof TypeError ? "" : error.message);
        complete.disabled = false;
      }
    });
  }
  document.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-remove-focus]")) return;
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
      if (!response.ok) throw new Error("Remove failed");
      await refresh();
    } catch {
      button.textContent = "Повторить";
    } finally {
      button.disabled = false;
    }
  });
})();
