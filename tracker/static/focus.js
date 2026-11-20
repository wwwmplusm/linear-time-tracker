(() => {
  const dialog = document.getElementById("focus-dialog");
  const input = document.getElementById("focus-query");
  const results = document.getElementById("focus-results");
  const status = document.getElementById("focus-status");
  let controller;
  let delay;
  let saving = false;
  const csrf = document.querySelector('.header-actions input[name="csrf"]').value;

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
    queueSearch(0);
  });
  input.addEventListener("input", () => queueSearch(180));
  dialog.addEventListener("close", () => {
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
      location.assign(url);
    } catch {
      status.textContent = "Не удалось добавить задачу в фокус. Попробуйте ещё раз.";
    } finally {
      saving = false;
      input.disabled = false;
    }
  });
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
