// Keep only the directory in this document's memory. Timer controls continue to use live state.
(() => {
  if (!document.querySelector(".linear-browser")) return;
  const selectors = [".linear-intro", ".linear-browser", "#workspace-nav", ".linear-saved"];
  const pages = new Map();
  const requests = new Map();
  const currentSession = document.querySelector('input[name="csrf"]').value;
  let navigation = 0;

  function remember(key, doc) {
    const timer = doc.getElementById("tracker-state");
    const browser = doc.querySelector(".linear-browser");
    if (!timer || !browser || doc.querySelector('input[name="csrf"]')?.value !== currentSession) return null;
    const page = {
      sections: selectors.map(selector => doc.querySelector(selector)?.cloneNode(true)),
      target: timer.dataset.target, stateUrl: timer.dataset.stateUrl,
      expires: performance.now() + 30_000,
    };
    pages.delete(key);
    if (pages.size >= 20) pages.delete(pages.keys().next().value);
    pages.set(key, page);
    return page;
  }

  function load(key) {
    const cached = pages.get(key);
    if (cached && cached.expires > performance.now()) return Promise.resolve(cached);
    if (requests.has(key)) return requests.get(key);
    const promise = fetch(key, { cache: "no-store", signal: AbortSignal.timeout(8000) })
      .then(async response => {
        if (!response.ok || response.redirected) return null;
        return remember(key, new DOMParser().parseFromString(await response.text(), "text/html"));
      })
      .catch(() => null)
      .finally(() => requests.delete(key));
    requests.set(key, promise);
    return promise;
  }

  function keyFor(link) {
    if (!link || link.target || link.hasAttribute("download")) return null;
    const url = new URL(link.href);
    if (url.origin !== location.origin || url.pathname !== "/linear" || url.hash) return null;
    for (const param of url.searchParams.keys()) {
      if (!["team", "project", "q", "before", "after"].includes(param)) return null;
    }
    return url.pathname + url.search;
  }

  async function navigate(key, push) {
    const sequence = ++navigation;
    const page = await load(key);
    if (sequence !== navigation) return;
    if (!page) {
      if (push) location.assign(key);
      else location.reload();
      return;
    }
    selectors.forEach((selector, index) => {
      const old = document.querySelector(selector);
      const replacement = page.sections[index]?.cloneNode(true);
      if (replacement && old) old.replaceWith(replacement);
      else if (replacement) document.querySelector("main").append(replacement);
      else old?.remove();
    });
    if (push) history.pushState(null, "", key);
    state.dataset.target = page.target;
    state.dataset.stateUrl = page.stateUrl;
    for (const form of state.querySelectorAll('form[method="post"]')) {
      const url = new URL(form.action);
      form.action = url.pathname + location.search;
    }
    document.querySelector(".nav-toggle")?.setAttribute("aria-expanded", "false");
    updateTimerStatus();
    const heading = document.querySelector(".linear-title h1");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    refresh();
  }

  remember(location.pathname + location.search, document);
  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const key = keyFor(event.target.closest("a[href]"));
    if (!key) return;
    event.preventDefault();
    navigate(key, key !== location.pathname + location.search);
  });

  // Fetch at most one destination ahead, only while the pointer rests on an internal link.
  let hover;
  document.addEventListener("pointerover", event => {
    clearTimeout(hover);
    const link = event.target.closest("a[href]");
    const key = keyFor(link);
    if (!key || event.pointerType !== "mouse" || navigator.connection?.saveData) return;
    hover = setTimeout(() => {
      if (link.matches(":hover") && !requests.size) load(key);
    }, 100);
  });
  window.addEventListener("popstate", () => {
    const key = location.pathname + location.search;
    if (key !== state.dataset.target) navigate(key, false);
    else navigation++;
  });
})();
