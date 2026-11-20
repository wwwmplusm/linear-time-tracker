(() => {
  const panelUrl = chrome.runtime.getURL("panel.html");
  const panelOrigin = panelUrl.slice(0, panelUrl.lastIndexOf("/"));
  let iframe;
  let previousPath;
  let expanded = false;
  let contentHeight = 55;
  let changeTimer;
  let lastNotification = 0;
  const issueLink = 'a[href*="/issue/"]';
  const issueNumber = /\b[A-Z][A-Z0-9_-]{0,24}-\d+\b/;
  const changes = new MutationObserver(records => {
    const affectsIssueList = records.some(record => {
      const element = record.target instanceof Element ? record.target : record.target.parentElement;
      if (element?.closest(issueLink)) return true;
      if (record.type === "attributes") return record.oldValue?.includes("/issue/");
      if (record.type === "characterData") {
        return issueNumber.test(record.target.data) || issueNumber.test(record.oldValue ?? "");
      }
      return [...record.addedNodes, ...record.removedNodes].some(node =>
        (node instanceof Element && (node.matches(issueLink) || node.querySelector(issueLink)))
        || issueNumber.test(node.textContent ?? ""),
      );
    });
    if (!affectsIssueList || changeTimer !== undefined) return;
    changeTimer = setTimeout(() => {
      changeTimer = undefined;
      lastNotification = performance.now();
      iframe?.contentWindow.postMessage({ type: "linear-timer-issues-changed", path: previousPath }, panelOrigin);
    }, Math.max(75, 350 - (performance.now() - lastNotification)));
  });

  function watchIssues() {
    changes.disconnect();
    clearTimeout(changeTimer);
    changeTimer = undefined;
    if (!expanded || !/^\/[^/]+\/project\/[^/]+\/issues(?:\/|$)/.test(previousPath)) return;
    changes.observe(document.body, {
      subtree: true, childList: true, characterData: true, characterDataOldValue: true,
      attributes: true, attributeFilter: ["href"], attributeOldValue: true,
    });
  }

  function syncSize() {
    if (!iframe) return;
    const compact = previousPath?.split("/")[2] === "project" ? 340 : 286;
    const width = Math.min(expanded ? 440 : compact, window.innerWidth - 20);
    const height = expanded ? Math.min(contentHeight, 570, window.innerHeight - 59) : 55;
    Object.assign(iframe.style, {
      position: "fixed", right: "10px", bottom: "44px", width: width + "px",
      height: height + "px", border: "0", zIndex: "2147483646",
      colorScheme: "normal", background: "transparent",
    });
  }

  function syncLocation() {
    const path = location.pathname;
    if (path === previousPath) return;
    previousPath = path;
    if (!/^\/[^/]+\/(issue|project)\/[^/]+/.test(path)) {
      iframe?.remove();
      iframe = undefined;
      expanded = false;
      watchIssues();
      return;
    }
    if (!iframe) {
      iframe = document.createElement("iframe");
      iframe.title = "Личный таймер для Linear";
      iframe.src = panelUrl + "?" + new URLSearchParams({ path });
      syncSize();
      document.body.append(iframe);
    } else {
      syncSize();
      iframe.contentWindow.postMessage({ type: "linear-timer-path", path }, panelOrigin);
    }
    watchIssues();
  }

  window.addEventListener("message", event => {
    if (event.source !== iframe?.contentWindow || event.origin !== panelOrigin) return;
    if (event.data?.type === "linear-timer-size") {
      const wasExpanded = expanded;
      expanded = event.data.expanded === true;
      contentHeight = Number.isFinite(event.data.height) ? Math.max(55, event.data.height) : 570;
      syncSize();
      if (expanded !== wasExpanded) watchIssues();
    }
  });
  window.addEventListener("resize", syncSize);
  window.addEventListener("popstate", syncLocation);
  window.setInterval(syncLocation, 350);
  syncLocation();
})();
