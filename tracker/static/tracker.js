let state = document.querySelector("#tracker-state");
const error = document.querySelector("#sync-error");
const target = state.dataset.target;
const stateUrl = state.dataset.stateUrl;
const linearActions = document.querySelectorAll("[data-linear-target]");
const indicator = document.querySelector("#timer-indicator-text");
let serverNow = Number(state.dataset.now);
let syncedAt = performance.now();
let pending = false;

const targetList = document.querySelector(".targets");
const currentTarget = targetList?.querySelector('[aria-current="page"]');
if (currentTarget && targetList.scrollWidth > targetList.clientWidth) {
  targetList.scrollLeft = currentTarget.offsetLeft - (targetList.clientWidth - currentTarget.offsetWidth) / 2;
}

function updateClocks() {
  const now = serverNow + performance.now() - syncedAt;
  for (const duration of state.querySelectorAll("[data-start]")) {
    const total = Number(duration.dataset.worked) + Math.max(0, now - Number(duration.dataset.start));
    const seconds = Math.floor(total / 1000);
    const parts = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60];
    duration.textContent = parts.map(part => String(part).padStart(2, "0")).join(":");
  }
}

function updateTimerStatus() {
  const active = new Map(Array.from(state.querySelectorAll("[data-active-target]"), row =>
    [row.dataset.activeTarget, row.hasAttribute("data-paused")],
  ));
  const paused = [...active.values()].filter(Boolean).length;
  indicator.textContent = active.size ? `Идут ${active.size - paused}${paused ? ` · На паузе ${paused}` : ""}` : "Таймеры";
  for (const actions of linearActions) {
    const form = actions.querySelector("form");
    const link = actions.querySelector("a");
    const running = active.has(actions.dataset.linearTarget);
    link.querySelector("[data-timer-state]").textContent = active.get(actions.dataset.linearTarget) ? "На паузе" : "Таймер идёт";
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
  try {
    const response = await fetch(stateUrl, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (response.status === 401) {
      location.replace(target);
      return;
    }
    if (!response.ok) throw new Error("Could not refresh timers");
    const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updated = documentFragment.querySelector("#tracker-state");
    serverNow = Number(updated.dataset.now);
    syncedAt = performance.now();
    if (state.dataset.revision !== updated.dataset.revision) {
      const focused = document.activeElement;
      const controlId = state.contains(focused) && focused.id;
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
  }
}

window.addEventListener("pageshow", event => {
  if (event.persisted && location.pathname === new URL(target, location.origin).pathname) location.reload();
  else refresh();
});
window.addEventListener("focus", refresh);
document.addEventListener("visibilitychange", refresh);
window.setInterval(refresh, 5000);
window.setInterval(updateClocks, 1000);
