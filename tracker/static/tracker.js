let state = document.querySelector("#tracker-state");
const error = document.querySelector("#sync-error");
const indicator = document.querySelector("#timer-indicator-text");
let serverNow = Number(state.dataset.now);
let syncedAt = performance.now();
let pending = false;

const navToggle = document.querySelector(".nav-toggle");
if (navToggle) {
  document.documentElement.classList.add("has-nav-toggle");
  navToggle.addEventListener("click", () => {
    navToggle.setAttribute("aria-expanded", String(navToggle.getAttribute("aria-expanded") !== "true"));
  });
}

const targetList = document.querySelector(".targets");
const currentTarget = targetList?.querySelector('[aria-current="page"]');
if (currentTarget && targetList.scrollWidth > targetList.clientWidth) {
  targetList.scrollLeft = currentTarget.offsetLeft - (targetList.clientWidth - currentTarget.offsetWidth) / 2;
}

function updateClocks() {
  const now = serverNow + performance.now() - syncedAt;
  for (const duration of state.querySelectorAll("[data-start]")) {
    const total = Number(duration.dataset.worked) + Math.max(0, now - Number(duration.dataset.start));
    const limit = Number(duration.dataset.limit);
    const seconds = limit ? Math.max(0, Math.ceil((limit - total) / 1000)) : Math.floor(total / 1000);
    if (limit && total >= limit) refresh();
    const parts = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60];
    duration.textContent = parts.map(part => String(part).padStart(2, "0")).join(":");
  }
}

function updateTimerStatus() {
  const active = new Map(Array.from(state.querySelectorAll("[data-active-target]"), row =>
    [row.dataset.activeTarget, row],
  ));
  const paused = [...active.values()].filter(row => row.hasAttribute("data-paused")).length;
  const statusLink = indicator.parentElement;
  statusLink.toggleAttribute("data-idle", !active.size);
  if (active.size) statusLink.setAttribute("href", "#active");
  else statusLink.removeAttribute("href");
  indicator.textContent = active.size ? `Идут ${active.size - paused}${paused ? ` · На паузе ${paused}` : ""}` : "Нет активных таймеров";
  for (const actions of document.querySelectorAll("[data-linear-target]")) {
    const form = actions.querySelector("form");
    const link = actions.querySelector("a");
    const running = active.has(actions.dataset.linearTarget);
    const row = active.get(actions.dataset.linearTarget);
    link.querySelector("[data-timer-state]").textContent = row?.hasAttribute("data-overtime") ? "Сверх таймера"
      : row?.hasAttribute("data-paused") ? "На паузе" : "Таймер идёт";
    if (form.hidden === running) continue;
    const wasFocused = actions.contains(document.activeElement);
    form.hidden = running;
    link.hidden = !running;
    if (wasFocused) (running ? link : form.querySelector("button")).focus({ preventScroll: true });
  }
}

async function refresh() {
  if (pending || document.hidden) return;
  pending = true;
  const stateUrl = state.dataset.stateUrl;
  try {
    const response = await fetch(stateUrl, { cache: "no-store", headers: { "X-Timer-Revision": state.dataset.revision }, signal: AbortSignal.timeout(5000) });
    if (stateUrl !== state.dataset.stateUrl) return;
    if (response.status === 401) {
      location.replace(state.dataset.target);
      return;
    }
    if (response.status === 204) {
      serverNow = Number(response.headers.get("X-Tracker-Now"));
      syncedAt = performance.now();
      error.hidden = true;
      updateClocks();
      return;
    }
    if (!response.ok) throw new Error("Could not refresh timers");
    const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updated = documentFragment.querySelector("#tracker-state");
    if (stateUrl !== state.dataset.stateUrl) return;
    serverNow = Number(updated.dataset.now);
    syncedAt = performance.now();
    if (state.dataset.revision !== updated.dataset.revision) {
      const focused = document.activeElement;
      const controlId = state.contains(focused) && focused.id;
      const updatedTargets = documentFragment.querySelector("#workspace-nav .targets");
      if (updatedTargets && targetList && targetList.innerHTML !== updatedTargets.innerHTML) {
        targetList.replaceChildren(...updatedTargets.childNodes);
      }
      state.replaceWith(updated);
      state = updated;
      if (controlId) {
        const control = document.getElementById(controlId)
          || document.querySelector("#target-action, .linear-search input[type=search], .linear-intro > .primary");
        control?.focus({ preventScroll: true });
      }
      updateTimerStatus();
    }
    error.hidden = true;
    updateClocks();
  } catch {
    error.hidden = false;
  } finally {
    pending = false;
    if (stateUrl !== state.dataset.stateUrl) refresh();
  }
}

window.addEventListener("pageshow", event => {
  if (event.persisted && location.pathname === new URL(state.dataset.target, location.origin).pathname) location.reload();
});
window.addEventListener("focus", refresh);
document.addEventListener("visibilitychange", refresh);
window.setInterval(refresh, 5000);
window.setInterval(updateClocks, 1000);

document.addEventListener("click", event => {
  const trigger = event.target.closest("[data-pomo]");
  if (!trigger) return;
  const dialog = document.getElementById("pomo-dialog");
  const form = dialog.querySelector("[data-pomo-form]");
  form.action = trigger.form.action;
  form.querySelector("[data-pomo-fields]").replaceChildren(
    ...Array.from(trigger.form.querySelectorAll('input[type="hidden"]'), input => input.cloneNode(true))
  );
  dialog.querySelector("[data-pomo-target]").textContent = trigger.dataset.pomo;
  dialog.showModal();
  form.elements.pomo_minutes.select();
});

const stopDialog = document.querySelector("#stop-dialog");
if (stopDialog) {
  const review = stopDialog.querySelector("form");
  const fields = review.elements;
  let measured;
  document.addEventListener("submit", event => {
    const form = event.target;
    if (!form.matches("[data-stop-elapsed]")) return;
    event.preventDefault();
    const duration = form.closest(".timer-actions").querySelector(".duration");
    const elapsed = duration.hasAttribute("data-start")
      ? Number(duration.dataset.worked) + Math.max(0, serverNow + performance.now() - syncedAt - Number(duration.dataset.start))
      : Number(form.dataset.stopElapsed);
    measured = Math.floor(elapsed / 1000);
    review.action = form.action;
    fields.hours.value = Math.floor(measured / 3600);
    fields.minutes.value = Math.floor(measured / 60) % 60;
    fields.seconds.value = measured % 60;
    const clock = [fields.hours, fields.minutes, fields.seconds].map(field => field.value.padStart(2, "0")).join(":");
    stopDialog.querySelector("[data-stop-summary]").textContent = form.dataset.stopName + " · " + clock;
    fields.hours.setCustomValidity("");
    stopDialog.showModal();
  });
  review.addEventListener("input", () => fields.hours.setCustomValidity(""));
  review.addEventListener("submit", event => {
    const total = Number(fields.hours.value) * 3600 + Number(fields.minutes.value) * 60 + Number(fields.seconds.value);
    if (total > measured) {
      event.preventDefault();
      fields.hours.setCustomValidity("Введите длительность в пределах времени таймера.");
      fields.hours.reportValidity();
    } else {
      fields.keep_seconds.value = total;
    }
  });
  stopDialog.querySelector("[data-stop-cancel]").addEventListener("click", () => stopDialog.close());
}
