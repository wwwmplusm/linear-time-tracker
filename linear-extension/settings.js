const local = "http://localhost:8765";
const input = document.getElementById("origin");
const message = document.getElementById("status");

chrome.storage.local.get("trackerOrigin").then(({ trackerOrigin = local }) => {
  input.value = trackerOrigin;
});

document.querySelector("form").addEventListener("submit", async event => {
  event.preventDefault();
  let url;
  try {
    url = new URL(input.value);
  } catch {
    message.textContent = "Укажите адрес трекера целиком, начиная с https://.";
    return;
  }
  if (url.origin !== local && (url.protocol !== "https:" || url.hostname === "linear.app")) {
    message.textContent = "Укажите HTTPS-адрес своего трекера.";
    return;
  }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    message.textContent = "Укажите адрес без пути и параметров.";
    return;
  }
  try {
    if (url.origin !== local && !await chrome.permissions.request({ origins: ["https://" + url.hostname + "/*"] })) {
      message.textContent = "Нужен доступ расширения к этому адресу.";
      return;
    }
    await chrome.storage.local.set({ trackerOrigin: url.origin });
    message.textContent = "Адрес сохранён. Обновите страницу Linear.";
  } catch {
    message.textContent = "Не удалось сохранить адрес.";
  }
});
