// A modified click keeps the browser's normal new-tab/window behavior.
document.addEventListener("click", async event => {
  const link = event.target.closest("a[data-linear-destination]");
  if (!link || !event.isTrusted || event.defaultPrevented || event.button !== 0 ||
      event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const href = link.href;
  if (new URL(href).origin !== "https://linear.app") return;
  event.preventDefault();
  try {
    if (await chrome.runtime.sendMessage({ action: "open-destination", href })) return;
  } catch {
    // The extension may have reloaded since this tracker page opened.
  }
  location.assign(href);
});
