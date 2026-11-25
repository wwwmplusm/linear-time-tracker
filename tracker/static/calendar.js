(() => {
  const calendar = document.querySelector("[data-calendar]");
  const scroll = calendar.querySelector(".cal-scroll");
  const days = [...calendar.querySelectorAll(".cal-day")];
  const { items: localDetails, focus: focusOrder } = JSON.parse(
    document.querySelector("#calendar-details").textContent
  );
  let details = localDetails;
  const tray = calendar.querySelector(".cal-tasks");
  const empty = calendar.querySelector(".cal-empty");
  const focus = new Set(focusOrder);
  const collator = new Intl.Collator("ru");
  const groups = calendar.querySelector("[data-group]");
  const search = calendar.querySelector('input[type="search"]');
  const areas = {
    work: ["Работа и доход", "#fa818d"], study: ["Личное обучение", "#ecd365"],
    health: ["Здоровье и тренировки", "#78d69b"], social: ["Время с близкими", "#84aaff"],
    reading: ["Чтение для себя", "#7fd7ee"], other: ["LifeOPS и другое", "#c49af4"],
  };
  const dialog = calendar.querySelector("dialog");
  const form = dialog.querySelector("form");
  const fields = form.elements;
  const deleteButton = form.querySelector("[data-delete]");
  const formStatus = form.querySelector(".cal-form-status");
  const status = calendar.querySelector(".cal-status");
  const today = calendar.dataset.today;
  const todayColumn = days.find((column) => column.dataset.day === today);
  const defaultDay = todayColumn ?? days[0];
  const minutesPerDay = 1440;
  const snap = 15;
  const defaultDuration = 60;
  let editing;
  let dragging;
  let dropZone;
  let pending = false;

  const navToggle = document.querySelector(".nav-toggle");
  if (navToggle && !document.documentElement.classList.contains("has-nav-toggle")) {
    document.documentElement.classList.add("has-nav-toggle");
    navToggle.addEventListener("click", () => {
      const expanded = navToggle.getAttribute("aria-expanded") === "true";
      navToggle.setAttribute("aria-expanded", String(!expanded));
    });
  }

  function timeString(minutes) {
    const hours = String(Math.floor(minutes / 60)).padStart(2, "0");
    return hours + ":" + String(minutes % 60).padStart(2, "0");
  }

  function nowMinute() {
    const timezone = calendar.dataset.timezone;
    const clock = new Intl.DateTimeFormat("ru", {
      timeZone: timezone || undefined, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(new Date());
    const [hour, minute] = clock.split(":").map(Number);
    return hour * 60 + minute;
  }

  function firstStart(day) {
    return day === today
      ? Math.min(minutesPerDay - defaultDuration, Math.ceil(nowMinute() / snap) * snap)
      : 9 * 60;
  }

  function position(minutes) {
    return (minutes / minutesPerDay * 100) + "%";
  }

  function readBlock(button) {
    return {
      id: button.dataset.block,
      target: button.dataset.target,
      day: button.parentElement.dataset.day,
      start: button.hasAttribute("data-start") ? Number(button.dataset.start) : null,
      duration: Number(button.dataset.duration),
      version: Number(button.dataset.version),
    };
  }

  function setColor(button, slug) {
    button.style.setProperty("--cal-color", areas[details[slug]?.area || "other"][1]);
  }

  function caption(item) {
    return [item.identifier, item.project, item.lesson === item.project ? "" : item.lesson,
      item.priority === "Без приоритета" ? "" : item.priority].filter(Boolean).join(" · ");
  }

  function labelBlock(button) {
    const block = readBlock(button);
    const title = button.querySelector(".cal-event-name");
    const item = details[block.target];
    if (item) title.textContent = item.name;
    const name = title.textContent;
    const when = block.start === null ? "Весь день" : timeString(block.start) + "–" + timeString(block.start + block.duration);
    setColor(button, block.target);
    button.title = [name, when, item && caption(item), item?.summary].filter(Boolean).join(" · ");
    button.setAttribute("aria-label", button.title + " · " + button.parentElement.getAttribute("aria-label").split(" · ").at(-1));
  }

  // Each group of intersecting intervals shares the same number of lanes.
  function layoutDay(day) {
    const events = [...day.querySelectorAll("[data-block]")]
      .map((button) => ({ button, ...readBlock(button) }))
      .sort((a, b) => a.start - b.start || b.duration - a.duration);
    let group = [];
    let laneEnds = [];
    let groupEnd = 0;

    function layoutGroup() {
      for (const event of group) {
        const style = event.button.style;
        const width = 100 / laneEnds.length;
        style.setProperty("--cal-top", position(event.start));
        style.setProperty("--cal-height", position(event.duration));
        style.setProperty("--cal-left", "calc(" + event.lane * width + "% + 1px)");
        style.setProperty("--cal-width", "calc(" + width + "% - 2px)");
        event.button.toggleAttribute("data-short", event.duration <= 30);
      }
    }

    for (const event of events) {
      if (event.start >= groupEnd) {
        layoutGroup();
        group = [];
        laneEnds = [];
      }
      const end = event.start + event.duration;
      let lane = laneEnds.findIndex((laneEnd) => laneEnd <= event.start);
      if (lane === -1) lane = laneEnds.length;
      laneEnds[lane] = end;
      groupEnd = Math.max(end, groupEnd);
      group.push({ ...event, lane });
    }
    layoutGroup();
  }

  calendar.querySelectorAll("[data-block]").forEach(labelBlock);
  days.forEach(layoutDay);

  function applyChange(id, block, deleting) {
    const previous = calendar.querySelector(`[data-block="${id}"]`);
    const oldDay = previous?.closest(".cal-day");
    previous?.remove();
    if (oldDay) layoutDay(oldDay);
    if (deleting) return;

    const zone = calendar.querySelector(
      (block.start === null ? ".cal-lane" : ".cal-day") + `[data-day="${block.day}"]`
    );
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cal-event";
    button.draggable = true;
    Object.assign(button.dataset, {
      block: id, target: block.target, duration: block.duration, version: block.version + 1,
    });
    if (block.start !== null) {
      button.dataset.start = block.start;
      const time = document.createElement("span");
      time.className = "cal-event-time";
      time.textContent = timeString(block.start);
      button.append(time);
    }
    const name = document.createElement("span");
    name.className = "cal-event-name";
    name.textContent = details[block.target]?.name || block.target;
    button.append(name);
    zone.append(button);
    labelBlock(button);
    if (block.start !== null) layoutDay(zone);
  }

  function groupName(item) {
    if (groups.value === "area") return areas[item.area][0];
    if (groups.value === "project") return item.project || "Без проекта";
    if (groups.value === "lesson") return item.lesson || "Другие задачи";
    return item.priority;
  }

  function indexTasks(order) {
    return order.map(slug => {
      const item = details[slug];
      const subtitle = caption(item);
      return {
        slug, item, subtitle,
        label: [item.name, subtitle, item.summary, areas[item.area][0]].join(" ").toLocaleLowerCase("ru"),
        row: null,
      };
    }).sort((a, b) =>
      Number(focus.has(b.slug)) - Number(focus.has(a.slug)) ||
      collator.compare(a.item.name, b.item.name));
  }

  function taskRow({ slug, item, subtitle }) {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cal-task";
    button.draggable = true;
    button.dataset.task = slug;
    button.toggleAttribute("data-focus", focus.has(slug));
    setColor(button, slug);
    const dot = document.createElement("span");
    dot.className = "cal-dot";
    dot.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.textContent = item.name;
    const captionText = document.createElement("small");
    captionText.textContent = subtitle;
    text.append(captionText);
    if (item.summary) {
      const summary = document.createElement("small");
      summary.className = "cal-summary";
      summary.textContent = item.summary;
      text.append(summary);
      button.title = item.summary;
    }
    button.append(dot, text);
    row.append(button);
    return row;
  }

  const localSlugs = Object.keys(localDetails);
  const sortedLocalSlugs = [...localSlugs].sort();
  let tasks = indexTasks(sortedLocalSlugs);
  let optionsReady = false;
  let optionOrder = [...focusOrder, ...localSlugs.filter(slug => !focus.has(slug))];

  function renderTasks() {
    const query = search.value.trim().toLocaleLowerCase("ru");
    const words = query ? query.split(/\s+/) : [];
    const sections = new Map();
    for (const task of tasks) {
      if (!words.every(word => task.label.includes(word))) continue;
      const group = groupName(task.item);
      if (!sections.has(group)) sections.set(group, []);
      task.row ??= taskRow(task);
      sections.get(group).push(task.row);
    }
    const list = document.createDocumentFragment();
    const priorities = ["Срочно", "Высокий", "Средний", "Низкий", "Без приоритета"];
    const ordered = [...sections].sort(([a], [b]) => groups.value === "priority"
      ? priorities.indexOf(a) - priorities.indexOf(b) : collator.compare(a, b));
    for (const [group, rows] of ordered) {
      const heading = document.createElement("li");
      heading.className = "cal-section";
      heading.textContent = group;
      list.append(heading);
      for (const row of rows) list.append(row);
    }
    tray.replaceChildren(list);
    empty.hidden = sections.size > 0;
  }
  renderTasks();
  search.addEventListener("input", renderTasks);
  groups.addEventListener("change", renderTasks);

  function describeSelection() {
    const item = details[fields.target.value];
    form.querySelector("[data-card-details]").textContent = item
      ? [areas[item.area][0], caption(item), item.summary].filter(Boolean).join(" · ") : "";
  }
  fields.target.addEventListener("change", describeSelection);
  if (calendar.hasAttribute("data-linear-catalog")) {
    let etag = "";
    let syncing = false;
    const feedback = calendar.querySelector("[data-sync-status]");
    async function sync() {
      if (syncing || document.hidden || dragging || dialog.open || pending) return;
      syncing = true;
      try {
        const response = await fetch("/calendar/catalog", { headers: etag ? { "X-Catalog-Revision": etag } : {} });
        if (response.status === 304) { feedback.textContent = ""; return; }
        if (!response.ok) throw new Error();
        const fresh = await response.json();
        if (dragging || dialog.open || pending) return;
        const used = new Set([...calendar.querySelectorAll("[data-block]")].map(block => block.dataset.target));
        const retained = Object.fromEntries(sortedLocalSlugs.filter(slug =>
          !slug.startsWith("linear-") || used.has(slug) || focus.has(slug))
          .map(slug => [slug, localDetails[slug]]));
        details = { ...retained, ...fresh };
        optionOrder = Object.keys(details);
        tasks = indexTasks(optionOrder);
        optionsReady = false;
        renderTasks();
        calendar.querySelectorAll("[data-block]").forEach(labelBlock);
        etag = response.headers.get("X-Catalog-Revision") || "";
        feedback.textContent = "";
      } catch {
        feedback.textContent = "Linear временно недоступен. Повторим обновление автоматически.";
      } finally {
        syncing = false;
      }
    }
    sync();
    setInterval(sync, 10000);
    window.addEventListener("focus", sync);
    document.addEventListener("visibilitychange", sync);
  }

  function updateTimeField() {
    const allDay = fields.all_day.checked;
    fields.time.disabled = allDay;
    form.querySelector("[data-time-field]").hidden = allDay;
  }

  function openEditor({ id, target, day, start, duration, version }) {
    if (pending || !optionOrder.length) return;
    if (!optionsReady) {
      const options = document.createDocumentFragment();
      for (const slug of optionOrder) options.append(new Option(details[slug].name, slug));
      fields.target.replaceChildren(options);
      optionsReady = true;
    }
    editing = { id, version };
    formStatus.hidden = true;
    dialog.querySelector("h2").textContent = id ? "Редактировать блок" : "Новый блок";
    deleteButton.hidden = !id;
    fields.target.value = target || fields.target.options[0].value;
    describeSelection();
    fields.day.value = day;
    fields.all_day.checked = start === null;
    fields.time.value = timeString(start ?? firstStart(day));
    fields.duration.value = duration;
    fields.duration.setCustomValidity("");
    updateTimeField();
    dialog.showModal();
    (target ? fields.day : fields.target).focus();
  }

  function newBlock(day, start, target) {
    openEditor({
      target, day, start,
      duration: start === null ? defaultDuration : Math.min(defaultDuration, minutesPerDay - start),
      version: 0,
    });
  }

  function slotAt(event, zone, duration, offset = 0) {
    const box = zone.getBoundingClientRect();
    const rawMinute = (event.clientY - box.top) / box.height * minutesPerDay - offset;
    const lastSlot = Math.floor((minutesPerDay - duration) / snap) * snap;
    return Math.max(0, Math.min(lastSlot, Math.floor(rawMinute / snap) * snap));
  }

  calendar.addEventListener("click", (event) => {
    const block = event.target.closest("[data-block]");
    const task = event.target.closest("[data-task]");
    const add = event.target.closest("[data-new], [data-new-day]");
    const zone = event.target.closest("[data-day]");
    if (block) {
      openEditor(readBlock(block));
    } else if (task || add) {
      const day = add?.dataset.newDay ?? defaultDay.dataset.day;
      newBlock(day, firstStart(day), task?.dataset.task);
    } else if (zone) {
      const start = zone.hasAttribute("data-all-day") ? null : slotAt(event, zone, snap);
      newBlock(zone.dataset.day, start);
    }
  });

  fields.all_day.addEventListener("change", updateTimeField);
  form.addEventListener("input", () => { fields.duration.setCustomValidity(""); });
  dialog.querySelector("[data-close]").addEventListener("click", () => { dialog.close(); });
  dialog.addEventListener("cancel", (event) => {
    if (pending) event.preventDefault();
  });

  async function mutate(block, deleting = false) {
    if (pending) return;
    pending = true;
    const message = dialog.open ? formStatus : status;
    message.hidden = true;
    const controls = dialog.querySelectorAll("button");
    controls.forEach((button) => { button.disabled = true; });

    try {
      const payload = new URLSearchParams({ csrf: fields.csrf.value, version: block.version });
      if (!deleting) {
        payload.set("target", block.target);
        payload.set("day", block.day);
        payload.set("duration", block.duration);
        if (block.start !== null) payload.set("start_minute", block.start);
      }
      const id = block.id ?? crypto.randomUUID();
      const url = "/calendar/blocks/" + encodeURIComponent(id) + (deleting ? "/delete" : "");
      const response = await fetch(url, { method: "POST", body: payload });
      if (response.status === 204) {
        if (!deleting && !days.some(column => column.dataset.day === block.day)) {
          location.assign("/calendar?week=" + encodeURIComponent(block.day));
        } else {
          if (dialog.open) dialog.close();
          applyChange(id, block, deleting);
          status.textContent = "";
        }
        return;
      }
      message.textContent = response.status === 409
        ? "Блок уже изменён. Обновите страницу и повторите действие."
        : "Не удалось сохранить изменение. Проверьте данные и повторите попытку.";
    } catch {
      message.textContent = "Не удалось отправить изменение. Проверьте подключение и обновите страницу.";
    } finally {
      pending = false;
      controls.forEach((button) => { button.disabled = false; });
    }
    message.hidden = false;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const [hours, minutes] = fields.time.value.split(":").map(Number);
    const start = fields.all_day.checked ? null : hours * 60 + minutes;
    const duration = Number(fields.duration.value);
    if (start !== null && start + duration > minutesPerDay) {
      fields.duration.setCustomValidity("Блок должен закончиться до полуночи.");
      fields.duration.reportValidity();
      return;
    }
    mutate({ ...editing, target: fields.target.value, day: fields.day.value, start, duration });
  });
  deleteButton.addEventListener("click", () => mutate(editing, true));

  function clearDrop() {
    dropZone?.removeAttribute("data-drop");
    dropZone = null;
  }

  calendar.addEventListener("dragstart", (event) => {
    const button = event.target.closest("[data-block], [data-task]");
    if (!button || pending) {
      event.preventDefault();
      return;
    }
    dragging = button.hasAttribute("data-block")
      ? readBlock(button)
      : { target: button.dataset.task, version: 0, duration: defaultDuration };
    const box = button.getBoundingClientRect();
    dragging.offset = dragging.start == null ? 0
      : Math.min(dragging.duration, Math.max(0, (event.clientY - box.top) / box.height * dragging.duration));
    event.dataTransfer.effectAllowed = dragging.id ? "move" : "copy";
    event.dataTransfer.setData("text/plain", dragging.target);
  });

  function dropStart(event, zone) {
    return zone.hasAttribute("data-all-day") ? null : slotAt(event, zone, dragging.duration, dragging.offset);
  }

  calendar.addEventListener("dragover", (event) => {
    const zone = event.target.closest("[data-day]");
    if (!dragging || pending || !zone) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = dragging.id ? "move" : "copy";
    if (dropZone !== zone) clearDrop();
    dropZone = zone;
    zone.setAttribute("data-drop", "");
    const start = dropStart(event, zone);
    if (start !== null) {
      zone.style.setProperty("--cal-drop-top", position(start));
      zone.style.setProperty("--cal-drop-height", position(dragging.duration));
    }
  });
  calendar.addEventListener("dragleave", (event) => {
    if (dropZone && !dropZone.contains(event.relatedTarget)) clearDrop();
  });
  calendar.addEventListener("dragend", () => {
    clearDrop();
    dragging = null;
  });
  calendar.addEventListener("drop", (event) => {
    const zone = event.target.closest("[data-day]");
    if (!dragging || pending || !zone) return;
    event.preventDefault();
    const day = zone.dataset.day;
    const start = dropStart(event, zone);
    const block = dragging;
    clearDrop();
    dragging = null;
    if (block.day !== day || block.start !== start) mutate({ ...block, day, start });
  });

  if (todayColumn) {
    const line = document.createElement("span");
    line.className = "cal-now";
    line.setAttribute("aria-hidden", "true");
    todayColumn.append(line);
    const updateNow = () => { line.style.top = position(nowMinute()); };
    updateNow();
    setInterval(updateNow, 60_000);
  }
  const hourHeight = days[0].offsetHeight / 24;
  const initialHour = todayColumn ? Math.max(7, Math.min(20, Math.floor(nowMinute() / 60) - 1)) : 8;
  scroll.scrollTop = initialHour * hourHeight;
  if (todayColumn && scroll.scrollWidth > scroll.clientWidth) {
    const gutter = calendar.querySelector(".cal-times").offsetWidth;
    scroll.scrollLeft = todayColumn.offsetLeft - gutter;
  }
})();
