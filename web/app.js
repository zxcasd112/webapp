(() => {
  "use strict";

  const tg = window.Telegram ? window.Telegram.WebApp : null;
  const $ = (id) => document.getElementById(id);
  const DEMO_USER = 999999;

  let state = {
    profile: { title: "slaughter_lord", sub: "Записывай. Кастомизируй. Побеждай.", label: "задача", accent: "#ff0d0d", mode: "blood" },
    projects: [], tags: [], tasks: [], habits: [], diary: [],
    stats: { done_total: 0, open_total: 0, done_today: 0, open_today: 0, pomo_minutes_today: 0, habit_count: 0, today: "" }
  };
  let demoMode = false;
  let currentView = "temple";
  let taskFilter = "all";
  let showArchive = false;
  let currentProjectId = null;
  let taskViewMode = "list";
  let calY = null;
  let calM = null;
  let calSel = todayStr();
  let pomoMinutes = 25;
  let pomoLeft = 25 * 60;
  let pomoTimer = null;
  let pomoRunning = false;

  function todayStr() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function addDaysStr(n) {
    const d = new Date();
    d.setDate(d.getDate() + n);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function apiUrl(path) {
    const q = tg && tg.initData ? "initData=" + encodeURIComponent(tg.initData) : "user_id=" + DEMO_USER;
    return "/api/" + path + "?" + q;
  }
  async function apiFetch(path, opts) {
    opts = opts || {};
    opts.headers = { "Content-Type": "application/json" };
    return fetch(apiUrl(path), opts);
  }
  function apiSave(path, data) {
    apiFetch(path, { method: "POST", body: JSON.stringify(data) }).catch(() => {});
  }
  function saveProfile() { apiSave("profile", state.profile); }
  function saveTask(t) { apiSave("tasks/" + t.id, t); }

  function fetchWithTimeout(url, opts, ms) {
    opts = opts || {};
    if (typeof AbortController === "undefined") return fetch(url, opts);
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), ms);
    opts.signal = c.signal;
    return fetch(url, opts).then(
      (r) => { clearTimeout(t); return r; },
      (e) => { clearTimeout(t); throw e; }
    );
  }

  async function loadFull() {
    try {
      const r = await fetchWithTimeout(apiUrl("full"), {}, 12000);
      if (!r.ok) throw new Error("api");
      state = await r.json();
      if (!state.stats) state.stats = blankStats();
    } catch (e) {
      demoMode = true;
      state.stats = blankStats();
      toast("DEMO: сервер просыпается");
      setTimeout(retryLoad, 20000);
    }
    applyProfile();
    renderAll();
  }

  async function retryLoad() {
    if (!demoMode) return;
    try {
      const r = await fetchWithTimeout(apiUrl("full"), {}, 15000);
      if (!r.ok) throw new Error("api");
      state = await r.json();
      if (!state.stats) state.stats = blankStats();
      demoMode = false;
      applyProfile();
      renderAll();
      toast("Сервер проснулся");
    } catch (e) {
      setTimeout(retryLoad, 30000);
    }
  }
  function blankStats() {
    return { done_total: 0, open_total: 0, done_today: 0, open_today: 0, pomo_minutes_today: 0, habit_count: 0, today: todayStr() };
  }

  function toast(msg) {
    const box = $("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => { el.remove(); }, 2800);
  }

  function switchView(name) {
    currentView = name;
    const views = document.querySelectorAll(".view");
    for (let i = 0; i < views.length; i++) views[i].hidden = true;
    const el = $("view-" + name);
    if (el) el.hidden = false;
    const tabs = document.querySelectorAll(".tab");
    for (let i = 0; i < tabs.length; i++) {
      if (tabs[i].dataset.view === name) tabs[i].classList.add("active");
      else tabs[i].classList.remove("active");
    }
    renderCurrentView();
    window.scrollTo(0, 0);
  }
  function renderAll() { renderCurrentView(); }
  function renderCurrentView() {
    if (currentView === "temple") renderTemple();
    else if (currentView === "tasks") renderTasks();
    else if (currentView === "habits") renderHabits();
    else if (currentView === "diary") renderDiary();
    else if (currentView === "stats") renderStats();
  }

  function parseSmart(text) {
    let due = null, scheduled = null;
    const t = " " + text.toLowerCase() + " ";
    const tm = t.match(/(\d{1,2}):(\d{2})/);
    if (tm) {
      const h = parseInt(tm[1], 10), m = parseInt(tm[2], 10);
      if (h < 24 && m < 60) scheduled = tm[1].padStart(2, "0") + ":" + tm[2];
    }
    if (t.indexOf("сегодня") !== -1) due = todayStr();
    else if (t.indexOf("послезавтра") !== -1) due = addDaysStr(2);
    else if (t.indexOf("завтра") !== -1) due = addDaysStr(1);
    else {
      const map = { "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7 };
      const nowDow = new Date().getDay() || 7;
      for (const k in map) {
        if (t.indexOf(k) !== -1) {
          let diff = map[k] - nowDow;
          if (diff <= 0) diff += 7;
          due = addDaysStr(diff);
          break;
        }
      }
    }
    const dm = t.match(/(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?/);
    if (dm) {
      let yr = dm[3] ? parseInt(dm[3], 10) : new Date().getFullYear();
      if (yr < 100) yr += 2000;
      due = yr + "-" + String(dm[2]).padStart(2, "0") + "-" + String(dm[1]).padStart(2, "0");
    }
    return { due: due, scheduled: scheduled };
  }

  function stripSmartWords(text) {
    return text
      .replace(/\b(сегодня|завтра|послезавтра|пн|вт|ср|чт|пт|сб|вс)\b/gi, " ")
      .replace(/\b\d{1,2}:\d{2}\b/g, " ")
      .replace(/\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b/g, " ")
      .replace(/#([^\s#]+)/g, " ")
      .replace(/\s+/g, " ").trim();
  }

  function smartAddTask(raw) {
    const sm = parseSmart(raw);
    let title = stripSmartWords(raw);
    let important = 0;
    if (title.charAt(0) === "!") { important = 1; title = title.slice(1).trim(); }
    if (!title) title = raw.trim();
    const tagIds = [];
    const tm = raw.match(/#([^\s#]+)/g) || [];
    for (let i = 0; i < tm.length; i++) {
      const nm = tm[i].slice(1).toLowerCase();
      const f = state.tags.find(x => x.name.toLowerCase() === nm);
      if (f && tagIds.indexOf(f.id) === -1) tagIds.push(f.id);
    }
    addTask({ title: title, due: sm.due, scheduled: sm.scheduled, important: important, tags: tagIds });
  }

  async function addTask(d) {
    const body = {
      title: d.title || "Новая задача", note: "", done: false, archived: false,
      priority: 0, important: d.important ? 1 : 0,
      due: d.due || null, scheduled: d.scheduled || null, repeat: "",
      project_id: currentProjectId, parent_id: null, tags: d.tags || []
    };
    try {
      const r = await apiFetch("tasks", { method: "POST", body: JSON.stringify(body) });
      if (!r.ok) throw new Error("x");
      const t = await r.json();
      state.tasks.push(t);
      state.stats.open_total++;
      toast("Добавлено");
    } catch (e) { toast("Ошибка добавления"); }
    renderAll();
  }

  async function toggleTask(id) {
    const t = state.tasks.find(x => x.id === id);
    if (!t) return;
    t.done = !t.done;
    if (t.done) { state.stats.done_total++; state.stats.open_total--; }
    else { state.stats.done_total--; state.stats.open_total++; }
    saveTask(t);
    renderAll();
  }

  async function delTask(id) {
    try {
      const r = await apiFetch("tasks/" + id, { method: "DELETE" });
      if (!r.ok) throw new Error("x");
      state.tasks = state.tasks.filter(x => x.id !== id);
      toast("Удалено");
    } catch (e) { toast("Ошибка удаления"); }
    renderAll();
  }

  function taskById(id) { return state.tasks.find(x => x.id === id); }
  function projById(id) { return state.projects.find(x => x.id === id); }

  function buildTaskEl(t) {
    const li = document.createElement("li");
    li.className = "task" + (t.done ? " done" : "") + (t.parent_id ? " sub" : "");
    const toggle = document.createElement("button");
    toggle.className = "task-toggle";
    toggle.textContent = "ok";
    toggle.setAttribute("aria-label", "Выполнить");
    toggle.addEventListener("click", (e) => { e.stopPropagation(); toggleTask(t.id); });
    const body = document.createElement("div");
    body.className = "task-body";
    body.addEventListener("click", () => openTaskModal(t));
    const titleEl = document.createElement("div");
    titleEl.className = "task-title";
    titleEl.textContent = t.title;
    body.appendChild(titleEl);
    const meta = document.createElement("div");
    meta.className = "task-meta";
    const today = todayStr();
    if (t.due) {
      const s = document.createElement("span");
      if (t.due === today) s.textContent = "сегодня";
      else if (t.due === addDaysStr(1)) s.textContent = "завтра";
      else s.textContent = t.due;
      if (t.due <= today && !t.done) s.style.color = "var(--accent)";
      meta.appendChild(s);
    }
    if (t.scheduled) {
      const s = document.createElement("span");
      s.textContent = t.scheduled;
      meta.appendChild(s);
    }
    const p = projById(t.project_id);
    if (p) {
      const s = document.createElement("span");
      s.textContent = (p.icon ? p.icon + " " : "") + p.name;
      meta.appendChild(s);
    }
    if (t.important) {
      const s = document.createElement("span");
      s.className = "task-prio";
      s.textContent = "!";
      meta.appendChild(s);
    }
    if (t.tags) {
      for (let i = 0; i < t.tags.length; i++) {
        const pill = document.createElement("span");
        pill.className = "tag-pill";
        pill.textContent = t.tags[i].name;
        pill.style.background = t.tags[i].color || "#b44dff";
        meta.appendChild(pill);
      }
    }
    body.appendChild(meta);
    const del = document.createElement("button");
    del.className = "task-del";
    del.textContent = "x";
    del.setAttribute("aria-label", "Удалить");
    del.addEventListener("click", (e) => { e.stopPropagation(); delTask(t.id); });
    li.appendChild(toggle);
    li.appendChild(body);
    li.appendChild(del);
    return li;
  }

  function renderTemple() {
    const s = state.stats || blankStats();
    $("templeName").textContent = state.profile.title.replace(/_/g, " ");
    const box = $("templeStats");
    box.innerHTML = "";
    const items = [
      [s.open_today || 0, "сегодня"],
      [s.done_today || 0, "выполнено"],
      [s.pomo_minutes_today || 0, "мин фокуса"],
      [s.done_total || 0, "всего"]
    ];
    for (let i = 0; i < items.length; i++) {
      const sp = document.createElement("span");
      sp.className = "stat";
      const b = document.createElement("b");
      b.textContent = items[i][0];
      sp.appendChild(b);
      sp.appendChild(document.createTextNode(" " + items[i][1]));
      box.appendChild(sp);
    }
    const today = todayStr();
    const list = state.tasks.filter(t => !t.archived && !t.done && (t.due === today || !t.due)).slice(0, 8);
    const ul = $("templeTasks");
    ul.innerHTML = "";
    for (let i = 0; i < list.length; i++) ul.appendChild(buildTaskEl(list[i]));
    $("templeEmpty").style.display = list.length ? "none" : "block";
    const strip = $("templeHabits");
    strip.innerHTML = "";
    if (!state.habits.length) { $("templeHabitsEmpty").style.display = "block"; return; }
    $("templeHabitsEmpty").style.display = "none";
    const show = state.habits.slice(0, 4);
    for (let i = 0; i < show.length; i++) {
      const h = show[i];
      const el = document.createElement("div");
      el.className = "mini";
      const b = document.createElement("b");
      b.textContent = calcStreak(h);
      const sp = document.createElement("span");
      sp.textContent = h.name;
      el.appendChild(b);
      el.appendChild(sp);
      strip.appendChild(el);
    }
  }

  function renderTasks() {
    const chips = $("projectChips");
    chips.innerHTML = "";
    const all = document.createElement("button");
    all.className = "chip" + (!currentProjectId ? " active" : "");
    all.textContent = "Все";
    all.addEventListener("click", () => { currentProjectId = null; renderTasks(); });
    chips.appendChild(all);
    for (let i = 0; i < state.projects.length; i++) {
      const pr = state.projects[i];
      const b = document.createElement("button");
      b.className = "chip" + (currentProjectId === pr.id ? " active" : "");
      b.textContent = (pr.icon ? pr.icon + " " : "") + pr.name;
      b.addEventListener("click", () => {
        currentProjectId = (currentProjectId === pr.id) ? null : pr.id;
        renderTasks();
      });
      chips.appendChild(b);
    }
    const today = todayStr();
    let tasks = state.tasks.filter(t => showArchive ? t.archived : !t.archived);
    if (taskViewMode === "list") {
      if (taskFilter === "today") tasks = tasks.filter(t => !t.done && t.due === today);
      else if (taskFilter === "upcoming") tasks = tasks.filter(t => !t.done && t.due && t.due > today);
      else if (taskFilter === "done") tasks = tasks.filter(t => t.done);
    }
    if (currentProjectId) tasks = tasks.filter(t => t.project_id === currentProjectId);
    tasks.sort((a, b) => (b.priority - a.priority) || ((a.due || "9999") < (b.due || "9999") ? -1 : 1));
    const ul = $("taskList");
    ul.innerHTML = "";
    const roots = tasks.filter(t => !t.parent_id);
    for (let i = 0; i < roots.length; i++) {
      ul.appendChild(buildTaskEl(roots[i]));
      const kids = tasks.filter(x => x.parent_id === roots[i].id);
      for (let j = 0; j < kids.length; j++) ul.appendChild(buildTaskEl(kids[j]));
    }
    $("taskEmpty").style.display = tasks.length ? "none" : "block";

    const isList = taskViewMode === "list";
    ul.style.display = isList ? "" : "none";
    $("taskEmpty").style.display = (isList && !tasks.length) ? "block" : "none";
    $("calWrap").hidden = taskViewMode !== "cal";
    $("kanbanWrap").hidden = taskViewMode !== "kanban";
    if (taskViewMode === "cal") renderCalendar(tasks);
    if (taskViewMode === "kanban") renderKanban(tasks);
  }

  function monthKey(y, m, d) {
    return y + "-" + String(m + 1).padStart(2, "0") + "-" + String(d).padStart(2, "0");
  }

  function renderCalendar(tasks) {
    const now = new Date();
    if (calY === null) { calY = now.getFullYear(); calM = now.getMonth(); }
    const label = new Date(calY, calM, 1).toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
    $("calLabel").textContent = label;
    const grid = $("calGrid");
    grid.innerHTML = "";
    const dows = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    for (let i = 0; i < 7; i++) {
      const h = document.createElement("div");
      h.className = "cal-dow";
      h.textContent = dows[i];
      grid.appendChild(h);
    }
    const first = (new Date(calY, calM, 1).getDay() || 7) - 1;
    const dim = new Date(calY, calM + 1, 0).getDate();
    const today = todayStr();
    const byDay = {};
    for (let i = 0; i < tasks.length; i++) {
      if (tasks[i].due) {
        if (!byDay[tasks[i].due]) byDay[tasks[i].due] = [];
        byDay[tasks[i].due].push(tasks[i]);
      }
    }
    for (let i = 0; i < first; i++) {
      const b = document.createElement("div");
      b.className = "cal-day blank";
      grid.appendChild(b);
    }
    for (let d = 1; d <= dim; d++) {
      const key = monthKey(calY, calM, d);
      const c = document.createElement("button");
      c.className = "cal-day";
      if (key === today) c.classList.add("today");
      if (key === calSel) c.classList.add("sel");
      const n = document.createElement("span");
      n.textContent = d;
      c.appendChild(n);
      if (byDay[key]) {
        const dot = document.createElement("span");
        dot.className = "cal-dot";
        dot.textContent = byDay[key].length > 9 ? "9+" : byDay[key].length;
        dot.style.width = "auto";
        dot.style.height = "auto";
        dot.style.borderRadius = "999px";
        dot.style.padding = "0 4px";
        dot.style.fontSize = "9px";
        dot.style.color = "#000";
        c.appendChild(dot);
        if (byDay[key].some(t => t.important)) c.classList.add("has-ice");
      }
      c.setAttribute("aria-label", key);
      c.addEventListener("click", () => { calSel = key; renderCalendar(tasks); });
      grid.appendChild(c);
    }
    $("calDayLabel").textContent = calSel === today ? "Сегодня" : calSel;
    const dl = $("calDayList");
    dl.innerHTML = "";
    const dayTasks = (byDay[calSel] || []).slice().sort((a, b) => (b.priority - a.priority));
    for (let i = 0; i < dayTasks.length; i++) dl.appendChild(buildTaskEl(dayTasks[i]));
    if (!dayTasks.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "В этот день тишина";
      dl.appendChild(p);
    }
  }

  function renderKanban(tasks) {
    const today = todayStr();
    const cols = [
      { name: "Новые", list: tasks.filter(t => !t.done && (!t.due || t.due > today)) },
      { name: "Сегодня", list: tasks.filter(t => !t.done && t.due && t.due <= today) },
      { name: "Готово", list: tasks.filter(t => t.done) }
    ];
    const box = $("kanbanCols");
    box.innerHTML = "";
    for (let i = 0; i < cols.length; i++) {
      const col = document.createElement("div");
      col.className = "kan-col";
      const head = document.createElement("div");
      head.className = "kan-head";
      const nm = document.createElement("span");
      nm.textContent = cols[i].name;
      const cnt = document.createElement("b");
      cnt.textContent = cols[i].list.length;
      head.appendChild(nm);
      head.appendChild(cnt);
      col.appendChild(head);
      const ul = document.createElement("ul");
      ul.className = "kan-list";
      const sorted = cols[i].list.slice().sort((a, b) => (b.priority - a.priority));
      for (let j = 0; j < sorted.length; j++) ul.appendChild(buildTaskEl(sorted[j]));
      col.appendChild(ul);
      box.appendChild(col);
    }
  }

  function calcStreak(h) {
    if (!h.days || !h.days.length) return 0;
    const set = {};
    for (let i = 0; i < h.days.length; i++) set[h.days[i]] = 1;
    let streak = 0;
    const cur = new Date();
    if (!set[todayStr()]) cur.setDate(cur.getDate() - 1);
    while (true) {
      const k = cur.getFullYear() + "-" + String(cur.getMonth() + 1).padStart(2, "0") + "-" + String(cur.getDate()).padStart(2, "0");
      if (set[k]) { streak++; cur.setDate(cur.getDate() - 1); }
      else break;
    }
    return streak;
  }

  async function toggleHabitDay(id, day) {
    try {
      const r = await apiFetch("habits/" + id, { method: "POST", body: JSON.stringify({ day: day }) });
      if (!r.ok) throw new Error("x");
      const upd = await r.json();
      for (let i = 0; i < state.habits.length; i++) {
        if (state.habits[i].id === id) state.habits[i] = upd;
      }
      toast(upd.days.indexOf(day) !== -1 ? "Отмечено" : "Снято");
    } catch (e) { toast("Ошибка"); }
    renderHabits();
  }

  async function addHabit(name) {
    const good = $("habitGood").checked ? 1 : 0;
    try {
      const r = await apiFetch("habits", { method: "POST", body: JSON.stringify({ name: name, good: good }) });
      if (!r.ok) throw new Error("x");
      state.habits.push(await r.json());
      toast("Привычка добавлена");
    } catch (e) { toast("Ошибка"); }
    renderHabits();
  }

  function renderHabits() {
    const box = $("habitList");
    box.innerHTML = "";
    if (!state.habits.length) { $("habitEmpty").style.display = "block"; return; }
    $("habitEmpty").style.display = "none";
    const dates = [];
    for (let i = 27; i >= 0; i--) dates.push(addDaysStr(-i));
    for (let i = 0; i < state.habits.length; i++) {
      const h = state.habits[i];
      const card = document.createElement("div");
      card.className = "habit-card";
      const head = document.createElement("div");
      head.className = "habit-head";
      const nm = document.createElement("div");
      nm.className = "habit-name";
      nm.textContent = (h.good ? "+ " : "- ") + h.name;
      const pc = document.createElement("div");
      pc.className = "habit-pct";
      pc.textContent = calcStreak(h) + " дн. серия";
      const del = document.createElement("button");
      del.className = "habit-del";
      del.textContent = "x";
      del.setAttribute("aria-label", "Удалить привычку");
      del.addEventListener("click", async () => {
        try {
          await apiFetch("habits/" + h.id, { method: "DELETE" });
          state.habits = state.habits.filter(x => x.id !== h.id);
        } catch (e) {}
        renderHabits();
      });
      head.appendChild(nm);
      head.appendChild(pc);
      head.appendChild(del);
      card.appendChild(head);
      const grid = document.createElement("div");
      grid.className = "habit-days";
      for (let j = 0; j < dates.length; j++) {
        const day = dates[j];
        const hit = h.days && h.days.indexOf(day) !== -1;
        const cell = document.createElement("div");
        cell.className = "habit-day";
        const btn = document.createElement("button");
        btn.textContent = day.slice(8);
        if (hit) btn.classList.add("hit");
        btn.style.setProperty("--c", h.color || "#b44dff");
        btn.setAttribute("aria-label", day);
        btn.addEventListener("click", () => toggleHabitDay(h.id, day));
        cell.appendChild(btn);
        grid.appendChild(cell);
      }
      card.appendChild(grid);
      box.appendChild(card);
    }
  }

  async function saveDiaryEntry() {
    const text = $("diaryInput").value.trim();
    if (!text) { toast("Пустая запись"); return; }
    let mood = null;
    const act = document.querySelector("[data-mood].active");
    if (act) mood = parseInt(act.dataset.mood, 10);
    try {
      const r = await apiFetch("diary", { method: "POST", body: JSON.stringify({ day: todayStr(), text: text, mood: mood }) });
      if (!r.ok) throw new Error("x");
      const entry = await r.json();
      const ix = state.diary.findIndex(d => d.day === entry.day);
      if (ix !== -1) state.diary[ix] = entry;
      else state.diary.unshift(entry);
      $("diaryInput").value = "";
      const ms = document.querySelectorAll("[data-mood]");
      for (let i = 0; i < ms.length; i++) ms[i].classList.remove("active");
      toast("Сохранено");
    } catch (e) { toast("Ошибка"); }
    renderDiary();
  }

  function renderDiary() {
    const box = $("diaryList");
    box.innerHTML = "";
    if (!state.diary.length) { $("diaryEmpty").style.display = "block"; return; }
    $("diaryEmpty").style.display = "none";
    const faces = ["", ":(", ":|", ":)", "FIRE"];
    const list = state.diary.slice(0, 20);
    for (let i = 0; i < list.length; i++) {
      const e = list[i];
      const el = document.createElement("div");
      el.className = "diary-entry";
      const dt = document.createElement("div");
      dt.className = "date";
      dt.textContent = e.day + (e.mood ? "  " + faces[e.mood] : "");
      const tx = document.createElement("div");
      tx.className = "text";
      tx.textContent = e.text;
      el.appendChild(dt);
      el.appendChild(tx);
      box.appendChild(el);
    }
  }

  function fmtTime(s) {
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function startPomo() {
    if (pomoRunning) return;
    pomoRunning = true;
    $("pomoToggle").textContent = "Стоп";
    const panel = document.querySelector(".pomo-panel");
    if (panel) panel.classList.add("pomo-running");
    pomoTimer = setInterval(() => {
      pomoLeft--;
      $("pomoDisplay").textContent = fmtTime(pomoLeft);
      if (pomoLeft <= 0) {
        stopPomo();
        apiSave("pomo", { minutes: pomoMinutes, task_id: null, completed: true });
        state.stats.pomo_minutes_today += pomoMinutes;
        toast("Фокус завершен: +" + pomoMinutes + " мин");
        renderStats();
      }
    }, 1000);
  }
  function stopPomo() {
    pomoRunning = false;
    clearInterval(pomoTimer);
    $("pomoToggle").textContent = "Старт";
    const panel = document.querySelector(".pomo-panel");
    if (panel) panel.classList.remove("pomo-running");
    pomoLeft = pomoMinutes * 60;
    $("pomoDisplay").textContent = fmtTime(pomoLeft);
  }

  function renderStats() {
    const s = state.stats || blankStats();
    $("statsGrid").innerHTML = "";
    const cards = [
      [s.done_total, "всего выполнено"],
      [s.open_total, "в работе"],
      [s.done_today, "убито сегодня"],
      [s.pomo_minutes_today, "мин фокуса"]
    ];
    for (let i = 0; i < cards.length; i++) {
      const c = document.createElement("div");
      c.className = "stat-card";
      const b = document.createElement("b");
      b.textContent = cards[i][0];
      const sp = document.createElement("span");
      sp.textContent = cards[i][1];
      c.appendChild(b);
      c.appendChild(sp);
      $("statsGrid").appendChild(c);
    }
    $("pomoDisplay").textContent = fmtTime(pomoLeft);
  }

  function applyProfile() {
    const p = state.profile;
    document.title = p.title;
    document.documentElement.style.setProperty("--accent", p.accent);
    document.body.dataset.mode = p.mode;
    $("mastheadTitle").textContent = p.title;
    $("mastheadSub").textContent = p.sub;
    $("templeName").textContent = p.title.replace(/_/g, " ");
    $("setTitle").value = p.title;
    $("setSub").value = p.sub;
    $("setLabel").value = p.label;
    $("setAccent").value = p.accent;
    $("mastheadBadge").textContent = demoMode ? "DEMO" : "RB3";
    const modes = document.querySelectorAll("[data-mode]");
    for (let i = 0; i < modes.length; i++) {
      if (modes[i].dataset.mode === p.mode) modes[i].classList.add("active");
      else modes[i].classList.remove("active");
    }
  }

  function openTaskModal(t) {
    const task = taskById(t.id);
    if (!task) return;
    $("modalTitle").textContent = task.title;
    const body = $("modalBody");
    body.innerHTML = "";
    function field(label, input) {
      const w = document.createElement("div");
      w.className = "modal-field";
      const s = document.createElement("span");
      s.textContent = label;
      w.appendChild(s);
      w.appendChild(input);
      body.appendChild(w);
      return input;
    }
    function txtInput(val, type) {
      const i = document.createElement("input");
      i.className = "task-input";
      i.style.width = "100%";
      if (type) i.type = type;
      i.value = val || "";
      return i;
    }
    const inTitle = field("Название", txtInput(task.title));
    const sel = document.createElement("select");
    sel.className = "task-input";
    sel.style.width = "100%";
    const opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = "Без проекта";
    sel.appendChild(opt0);
    for (let i = 0; i < state.projects.length; i++) {
      const o = document.createElement("option");
      o.value = state.projects[i].id;
      o.textContent = (state.projects[i].icon ? state.projects[i].icon + " " : "") + state.projects[i].name;
      if (task.project_id === state.projects[i].id) o.selected = true;
      sel.appendChild(o);
    }
    field("Проект", sel);
    const inDue = field("Дата", txtInput(task.due || "", "date"));
    const inSch = field("Время", txtInput(task.scheduled || "", "time"));
    const row = document.createElement("div");
    row.className = "modal-row";
    const bPrio = document.createElement("button");
    bPrio.className = "btn btn-ghost";
    bPrio.textContent = "Приоритет: " + (task.priority || 0);
    bPrio.addEventListener("click", () => {
      task.priority = ((task.priority || 0) + 1) % 4;
      bPrio.textContent = "Приоритет: " + task.priority;
      saveTask(task);
      renderAll();
    });
    const bImp = document.createElement("button");
    bImp.className = "btn btn-ghost";
    bImp.textContent = task.important ? "Важно: да" : "Важно: нет";
    bImp.addEventListener("click", () => {
      task.important = task.important ? 0 : 1;
      bImp.textContent = task.important ? "Важно: да" : "Важно: нет";
      saveTask(task);
      renderAll();
    });
    row.appendChild(bPrio);
    row.appendChild(bImp);
    body.appendChild(row);
    const row2 = document.createElement("div");
    row2.className = "modal-row";
    const bSub = document.createElement("button");
    bSub.className = "btn btn-ghost";
    bSub.textContent = "+ Подзадача";
    bSub.addEventListener("click", async () => {
      try {
        const r = await apiFetch("tasks", { method: "POST", body: JSON.stringify({ title: "Новая подзадача", parent_id: task.id }) });
        if (r.ok) { state.tasks.push(await r.json()); toast("Подзадача добавлена"); }
      } catch (e) {}
      closeModal();
      renderAll();
    });
    const bArch = document.createElement("button");
    bArch.className = "btn btn-ghost";
    bArch.textContent = task.archived ? "Из архива" : "В архив";
    bArch.addEventListener("click", () => {
      task.archived = !task.archived;
      saveTask(task);
      closeModal();
      renderAll();
    });
    const bDel = document.createElement("button");
    bDel.className = "btn btn-ghost";
    bDel.textContent = "Удалить";
    bDel.addEventListener("click", () => { closeModal(); delTask(task.id); });
    row2.appendChild(bSub);
    row2.appendChild(bArch);
    row2.appendChild(bDel);
    body.appendChild(row2);
    inTitle.addEventListener("change", () => { task.title = inTitle.value.trim() || task.title; saveTask(task); renderAll(); });
    sel.addEventListener("change", () => { task.project_id = sel.value ? parseInt(sel.value, 10) : null; saveTask(task); renderAll(); });
    inDue.addEventListener("change", () => { task.due = inDue.value || null; saveTask(task); renderAll(); });
    inSch.addEventListener("change", () => { task.scheduled = inSch.value || null; saveTask(task); renderAll(); });
    $("modal").hidden = false;
  }
  function closeModal() { $("modal").hidden = true; }

  function bindAll() {
    const tabs = document.querySelectorAll(".tab");
    for (let i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", () => switchView(tabs[i].dataset.view));
    }
    $("templeQuickForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("templeQuickInput").value.trim();
      if (v) { smartAddTask(v); $("templeQuickInput").value = ""; }
    });
    $("taskAddForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("taskAddInput").value.trim();
      if (v) { smartAddTask(v); $("taskAddInput").value = ""; }
    });
    const filters = document.querySelectorAll("[data-filter]");
    for (let i = 0; i < filters.length; i++) {
      filters[i].addEventListener("click", () => {
        taskFilter = filters[i].dataset.filter;
        for (let j = 0; j < filters.length; j++) {
          if (filters[j] === filters[i]) filters[j].classList.add("active");
          else filters[j].classList.remove("active");
        }
        renderTasks();
      });
    }
    $("archiveBtn").addEventListener("click", () => {
      showArchive = !showArchive;
      if (showArchive) $("archiveBtn").classList.add("active");
      else $("archiveBtn").classList.remove("active");
      renderTasks();
    });
    const tviews = document.querySelectorAll("[data-tview]");
    for (let i = 0; i < tviews.length; i++) {
      tviews[i].addEventListener("click", () => {
        taskViewMode = tviews[i].dataset.tview;
        for (let j = 0; j < tviews.length; j++) {
          if (tviews[j] === tviews[i]) tviews[j].classList.add("active");
          else tviews[j].classList.remove("active");
        }
        renderTasks();
      });
    }
    $("calPrev").addEventListener("click", () => {
      if (calM === null) { const n = new Date(); calY = n.getFullYear(); calM = n.getMonth(); }
      if (calM === 0) { calM = 11; calY--; }
      else calM--;
      renderTasks();
    });
    $("calNext").addEventListener("click", () => {
      if (calM === null) { const n = new Date(); calY = n.getFullYear(); calM = n.getMonth(); }
      if (calM === 11) { calM = 0; calY++; }
      else calM++;
      renderTasks();
    });
    $("habitForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("habitInput").value.trim();
      if (v) { addHabit(v); $("habitInput").value = ""; }
    });
    $("diarySave").addEventListener("click", saveDiaryEntry);
    const moods = document.querySelectorAll("[data-mood]");
    for (let i = 0; i < moods.length; i++) {
      moods[i].addEventListener("click", () => {
        for (let j = 0; j < moods.length; j++) moods[j].classList.remove("active");
        moods[i].classList.add("active");
      });
    }
    const mins = document.querySelectorAll("[data-min]");
    for (let i = 0; i < mins.length; i++) {
      mins[i].addEventListener("click", () => {
        pomoMinutes = parseInt(mins[i].dataset.min, 10);
        pomoLeft = pomoMinutes * 60;
        for (let j = 0; j < mins.length; j++) {
          if (mins[j] === mins[i]) mins[j].classList.add("active");
          else mins[j].classList.remove("active");
        }
        $("pomoDisplay").textContent = fmtTime(pomoLeft);
      });
    }
    const firstMin = document.querySelector("[data-min]");
    if (firstMin) firstMin.classList.add("active");
    $("pomoToggle").addEventListener("click", () => {
      if (pomoRunning) stopPomo();
      else startPomo();
    });
    $("setTitle").addEventListener("input", (e) => { state.profile.title = e.target.value || "slaughter_lord"; applyProfile(); saveProfile(); });
    $("setSub").addEventListener("input", (e) => { state.profile.sub = e.target.value; applyProfile(); saveProfile(); });
    $("setLabel").addEventListener("input", (e) => { state.profile.label = e.target.value.trim() || "задача"; saveProfile(); renderAll(); });
    $("setAccent").addEventListener("input", (e) => {
      state.profile.accent = e.target.value;
      document.documentElement.style.setProperty("--accent", e.target.value);
      saveProfile();
    });
    const modes = document.querySelectorAll("[data-mode]");
    for (let i = 0; i < modes.length; i++) {
      modes[i].addEventListener("click", () => {
        state.profile.mode = modes[i].dataset.mode;
        applyProfile();
        saveProfile();
      });
    }
    $("resetBtn").addEventListener("click", () => {
      state.profile = { title: "slaughter_lord", sub: "Записывай. Кастомизируй. Побеждай.", label: "задача", accent: "#ff0d0d", mode: "blood" };
      applyProfile();
      saveProfile();
      toast("Профиль сброшен");
    });
    $("clearDone").addEventListener("click", async () => {
      const done = state.tasks.filter(t => t.done && !t.archived);
      for (let i = 0; i < done.length; i++) {
        done[i].archived = true;
        saveTask(done[i]);
      }
      toast("Выполненные — в архив");
      renderAll();
    });
    $("modalClose").addEventListener("click", closeModal);
    $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  }

  function boot() {
    window.__booted = true;
    if (tg) {
      try {
        tg.ready();
        tg.expand();
        tg.setHeaderColor("#0a0608");
        tg.setBackgroundColor("#0a0608");
      } catch (e) {}
    }
    bindAll();
    // мгновенный первый рендер, данные подтянутся фоном
    applyProfile();
    switchView("temple");
    loadFull();
  }

  document.addEventListener("DOMContentLoaded", boot);
/*__MORE2__*/
})();
