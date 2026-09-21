/* ==========================================================================
   slaughter_lord — клиент WebApp.

   Архитектура (без сборщика, обычный ES-код):

   * store   — состояние + локальный кеш (localStorage) + outbox изменений;
   * net     — один fetch-хелпер: батч /api/sync и условный GET /api/full (ETag);
   * render  — точечные обновления: узлы переиспользуются по ключу (keyed patch),
               перерисовка ставится в requestAnimationFrame и выполняется один
               раз на кадр;
   * events  — делегирование: один обработчик клика на документ вместо сотен
               слушателей на элементах списка;
   * outbox  — действия применяются локально (оптимистично) и уходят на сервер
               пачкой в фоне, поэтому интерфейс отзывчив даже на медленной сети
               и при «засыпании» бесплатного хостинга.
   ========================================================================== */

(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const CACHE_KEY = "tj.cache.v2";
  const QUEUE_KEY = "tj.queue.v2";
  const ETAG_KEY = "tj.etag.v2";
  const DEMO_USER = 999999;

  const VIEWS = ["temple", "tasks", "habits", "diary", "stats", "cult"];

  const DEFAULT_PROFILE = {
    title: "slaughter_lord",
    sub: "Записывай. Кастомизируй. Побеждай.",
    label: "задача",
    accent: "#ff0d0d",
    mode: "blood",
  };

  const $ = (id) => document.getElementById(id);
  const tplTask = $("tpl-task");
  const tplHabit = $("tpl-habit");

  // ---- авторизация: Telegram initData либо локальный демо-режим ------------
  const initData = tg && tg.initData ? tg.initData : "";
  const localHosts = ["localhost", "127.0.0.1", "::1", ""];
  const demoMode = !initData && localHosts.indexOf(location.hostname) !== -1;
  const authQuery = demoMode ? "?user_id=" + DEMO_USER : "";
  const authReady = Boolean(initData) || demoMode;

  // ---- состояние ----------------------------------------------------------
  const state = {
    profile: Object.assign({}, DEFAULT_PROFILE),
    projects: [],
    tags: [],
    tasks: [],
    habits: [],
    diary: [],
    stats: emptyStats(),
    etag: null,
  };

  const ui = {
    view: "temple",
    filter: "all",
    archive: false,
    tview: "list",
    projectId: null,
    calY: null,
    calM: null,
    calSel: today(),
    calSig: "",
    mood: null,
    modalTaskId: null,
    pomo: { minutes: 25, endAt: 0, running: false },
  };

  let queue = { ops: [], profile: null };
  let syncing = false;
  let retryTimer = null;
  let cacheTimer = null;
  let rafId = 0;
  const dirty = {
    masthead: true, temple: true, tasks: true, habits: true,
    diary: true, stats: true, cult: true,
  };
  const tempIds = {}; // cid -> временный id задачи
  const tagIdMap = {}; // временный id тега -> серверный

  // ---- мелкие утилиты -----------------------------------------------------
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function today() {
    const d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function shiftDays(days, from) {
    const d = from ? new Date(from + "T00:00:00") : new Date();
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  function text(node, value) {
    const next = value == null ? "" : String(value);
    if (node && node.textContent !== next) node.textContent = next;
  }

  function pluralForms(form) {
    const lower = String(form).toLowerCase();
    if (lower === "задача") return ["задача", "задачи", "задач"];
    if (lower === "привычка") return ["привычка", "привычки", "привычек"];
    if (lower === "запись") return ["запись", "записи", "записей"];
    return [form, form, form];
  }

  function plural(count, form) {
    const forms = pluralForms(form);
    const mod100 = count % 100;
    const mod10 = count % 10;
    if (mod10 === 1 && mod100 !== 11) return count + " " + forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return count + " " + forms[1];
    return count + " " + forms[2];
  }

  function emptyStats() {
    return {
      done_total: 0, open_total: 0, done_today: 0, open_today: 0, overdue: 0,
      pomo_minutes_today: 0, pomo_count_today: 0, habit_count: 0, today: today(),
    };
  }

  function findTask(id) {
    const key = String(id);
    for (let i = 0; i < state.tasks.length; i++) {
      if (String(state.tasks[i].id) === key) return state.tasks[i];
    }
    return null;
  }

  function findHabit(id) {
    const key = String(id);
    for (let i = 0; i < state.habits.length; i++) {
      if (String(state.habits[i].id) === key) return state.habits[i];
    }
    return null;
  }

  // ---- локальный кеш: мгновенный старт ------------------------------------
  function restore() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        const cached = saved && saved.state;
        if (cached) {
          state.profile = Object.assign({}, DEFAULT_PROFILE, cached.profile || {});
          state.projects = cached.projects || [];
          state.tags = cached.tags || [];
          state.tasks = cached.tasks || [];
          state.habits = cached.habits || [];
          state.diary = cached.diary || [];
          state.stats = Object.assign(emptyStats(), cached.stats || {});
        }
      }
      const rawQueue = localStorage.getItem(QUEUE_KEY);
      if (rawQueue) {
        const savedQueue = JSON.parse(rawQueue);
        if (savedQueue && Array.isArray(savedQueue.ops)) {
          queue = { ops: savedQueue.ops, profile: savedQueue.profile || null };
        }
      }
    } catch (e) { /* повреждённый кеш не должен ломать запуск */ }
  }

  function persistCache() {
    cacheTimer = null;
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify({
        ts: Date.now(),
        state: {
          profile: state.profile, projects: state.projects, tags: state.tags,
          tasks: state.tasks, habits: state.habits,
          diary: state.diary, stats: state.stats,
        },
      }));
    } catch (e) { /* переполнение хранилища игнорируем */ }
  }

  // Запись в localStorage отложена: она не должна попадать в кадр тапа.
  function touchCache() {
    if (cacheTimer) return;
    cacheTimer = setTimeout(persistCache, 700);
  }

  function persistQueue() {
    try { localStorage.setItem(QUEUE_KEY, JSON.stringify(queue)); } catch (e) {}
  }

  // ---- сеть ---------------------------------------------------------------
  function headers(extra) {
    const result = Object.assign({ "Content-Type": "application/json" }, extra || {});
    if (initData) result.Authorization = "tma " + initData;
    return result;
  }

  function withTimeout(request, ms, controller) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (controller) controller.abort();
        reject(new Error("timeout"));
      }, ms);
      request.then(
        (value) => { clearTimeout(timer); resolve(value); },
        (error) => { clearTimeout(timer); reject(error); }
      );
    });
  }

  async function api(path, options) {
    const opts = options || {};
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const request = fetch(path + authQuery, {
      method: opts.method || "GET",
      headers: headers(opts.headers),
      body: opts.body || null,
      signal: controller ? controller.signal : undefined,
      credentials: "same-origin",
      cache: "no-store",
    });
    const response = await withTimeout(request, opts.timeout || 15000, controller);
    let payload = null;
    if (response.status !== 304 && response.status !== 204) {
      const raw = await response.text();
      payload = raw ? JSON.parse(raw) : null;
    }
    return { status: response.status, data: payload, etag: response.headers.get("ETag") };
  }

  function setOnline(ok) {
    const badge = $("netBadge");
    if (badge) badge.hidden = ok;
  }

  function scheduleRetry(delay) {
    if (retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      syncNow();
    }, delay || 8000);
  }

  /**
   * Основной цикл синхронизации: сначала отдаём накопленные изменения,
   * затем забираем серверное состояние условным запросом (ETag → 304).
   */
  async function syncNow() {
    if (syncing) return;
    syncing = true;
    try {
      if (queue.ops.length || queue.profile) await flushQueue();
      if (!queue.ops.length && !queue.profile) await pull();
      setOnline(true);
    } catch (e) {
      setOnline(false);
      scheduleRetry();
    } finally {
      syncing = false;
    }
  }

  async function pull() {
    const cachedEtag = localStorage.getItem(ETAG_KEY);
    const res = await api("/api/full", {
      headers: cachedEtag ? { "If-None-Match": cachedEtag } : null,
      timeout: 15000,
    });
    if (res.status === 304) return false;
    if (res.status !== 200 || !res.data) throw new Error("full " + res.status);
    if (res.etag) {
      try { localStorage.setItem(ETAG_KEY, res.etag); } catch (e) {}
    }
    applyServerState(res.data);
    return true;
  }

  function applyServerState(data) {
    state.profile = Object.assign({}, DEFAULT_PROFILE, data.profile || {});
    state.projects = data.projects || [];
    state.tags = data.tags || [];
    state.tasks = data.tasks || [];
    state.habits = data.habits || [];
    state.diary = data.diary || [];
    state.stats = Object.assign(emptyStats(), data.stats || {});
    markAllDirty();
    applyProfile();
    touchCache();
  }

  // Отправка накопленной пачки. При любом сбое операции возвращаются
  // в очередь, поэтому ни одно действие пользователя не теряется.
  async function flushQueue() {
    if (!queue.ops.length && !queue.profile) return;
    const payload = { ops: queue.ops.slice(0, 200), profile: queue.profile };
    queue = { ops: queue.ops.slice(200), profile: null };
    persistQueue();

    let res = null;
    try {
      res = await api("/api/sync", {
        method: "POST",
        body: JSON.stringify(payload),
        timeout: 20000,
      });
    } catch (e) {
      restorePayload(payload);
      throw e;
    }
    if (res.status !== 200 || !res.data) {
      restorePayload(payload);
      throw new Error("sync " + res.status);
    }
    reconcile(res.data);
  }

  function restorePayload(payload) {
    queue = {
      ops: payload.ops.concat(queue.ops),
      profile: payload.profile || queue.profile,
    };
    persistQueue();
  }

  function reconcile(data) {
    const results = data.results || [];
    for (let i = 0; i < results.length; i++) applyResult(results[i]);
    if (data.stats) state.stats = Object.assign(emptyStats(), data.stats);
    if (data.profile) state.profile = Object.assign({}, state.profile, data.profile);
    applyProfile();
    markAllDirty();
    touchCache();
  }

  function applyResult(r) {
    if (!r || !r.t) return;
    if (r.t === "task.add" && r.item) return replaceTaskById(tempIds[r.cid], r.item, r.cid);
    if (r.t === "task.update" && r.item) return replaceTaskById(r.item.id, r.item, null);
    if (r.t === "habit.add" && r.item) return replaceHabitById(tempIds[r.cid], r.item);
    if (r.t === "habit.toggle" && r.item) return upsertHabit(r.item);
    if (r.t === "diary.save" && r.item) return upsertDiary(r.item);
    if (r.t === "project.add" && r.item) return replaceProjectById(tempIds[r.cid], r.item);
    if (r.t === "tag.add" && r.item) return resolveTag(r);
  }

  function replaceTaskById(id, item, cid) {
    if (cid && tempIds[cid]) delete tempIds[cid];
    if (id == null) return;
    for (let i = 0; i < state.tasks.length; i++) {
      if (state.tasks[i].id === id) {
        state.tasks[i] = item;
        return;
      }
    }
    state.tasks.push(item);
  }

  function upsertHabit(item) {
    if (!item) return;
    for (let i = 0; i < state.habits.length; i++) {
      if (state.habits[i].id === item.id) { state.habits[i] = item; return; }
    }
    state.habits.push(item);
  }

  function upsertDiary(item) {
    if (!item) return;
    for (let i = 0; i < state.diary.length; i++) {
      if (state.diary[i].day === item.day) { state.diary[i] = item; return; }
    }
    state.diary.push(item);
    state.diary.sort((a, b) => (a.day < b.day ? 1 : -1));
  }

  function upsertById(list, item) {
    for (let i = 0; i < list.length; i++) {
      if (list[i].id === item.id) { list[i] = item; return; }
    }
    list.push(item);
  }

  // Сервер вернул настоящий id тега, который мы создали локально: подменяем
  // его в задачах и отправляем уточнение, чтобы теги не «потерялись».
  function resolveTag(r) {
    const tempId = r.cid;
    if (!tempId) return;
    tagIdMap[tempId] = r.item.id;
    for (let i = 0; i < state.tags.length; i++) {
      if (state.tags[i].id === tempId) { state.tags[i] = r.item; break; }
    }
    for (let i = 0; i < state.tasks.length; i++) {
      const task = state.tasks[i];
      if (!task.tags || !task.tags.length) continue;
      let changed = false;
      for (let j = 0; j < task.tags.length; j++) {
        if (task.tags[j].id === tempId) { task.tags[j] = r.item; changed = true; }
      }
      if (changed && !task._pending) {
        queueTaskPatch(task, { tags: task.tags.map((tag) => tag.id) });
      }
    }
  }

  // ---- outbox -------------------------------------------------------------
  let flushTimer = null;

  function queueOps(op) {
    queue.ops.push(op);
    persistQueue();
    scheduleFlush();
  }

  // Задержка склеивает серию быстрых тапов в один сетевой запрос.
  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      syncNow();
    }, 350);
  }

  /**
   * Изменение существующей задачи. Если её создание ещё висит в очереди,
   * правка «вклеивается» в ту же операцию — лишних запросов не будет.
   */
  function queueTaskPatch(task, patch) {
    for (let i = 0; i < queue.ops.length; i++) {
      const op = queue.ops[i];
      if (op.cid && op.cid === task._cid && op.t === "task.add") {
        Object.assign(op.data, patch);
        persistQueue();
        scheduleFlush();
        return;
      }
    }
    queueOps({ t: "task.update", id: task.id, data: patch });
  }

  // ---- ядро отрисовки -----------------------------------------------------
  function markDirty(name) {
    dirty[name] = true;
    scheduleRender();
  }

  function markAllDirty() {
    for (let i = 0; i < VIEWS.length; i++) dirty[VIEWS[i]] = true;
    dirty.masthead = true;
    scheduleRender();
  }

  // Перерисовка ровно один раз на кадр, сколько бы изменений ни пришло.
  function scheduleRender() {
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      render();
    });
  }

  function render() {
    if (dirty.masthead) {
      renderMasthead();
      dirty.masthead = false;
    }
    if (dirty[ui.view]) {
      renderView(ui.view);
      dirty[ui.view] = false;
    }
  }

  function renderView(name) {
    if (name === "temple") renderTemple();
    else if (name === "tasks") renderTasks();
    else if (name === "habits") renderHabits();
    else if (name === "diary") renderDiary();
    else if (name === "stats") renderStats();
    else if (name === "cult") renderCult();
  }

  /**
   * Keyed-патчинг: существующие узлы переиспользуются, пересоздаются только
   * изменившиеся. Это убирает «мигание» списка и прыжки скролла, которые даёт
   * пересборка innerHTML на каждое действие.
   */
  function patchChildren(container, entries, build) {
    const index = container._index || (container._index = new Map());
    const alive = new Set();
    let anchor = container.firstElementChild;

    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      alive.add(entry.key);
      let record = index.get(entry.key);
      if (record && record.sig === entry.sig) {
        if (record.el !== anchor) container.insertBefore(record.el, anchor);
      } else {
        const el = build(entry);
        if (record) container.replaceChild(el, record.el);
        else container.insertBefore(el, anchor);
        record = { el: el, sig: entry.sig };
        index.set(entry.key, record);
      }
      anchor = record.el.nextElementSibling;
    }

    index.forEach((record, key) => {
      if (!alive.has(key)) {
        record.el.remove();
        index.delete(key);
      }
    });
  }

  function resetContainer(container) {
    container._index = new Map();
    container.textContent = "";
  }

  // ---- уведомления --------------------------------------------------------
  function toast(message, kind) {
    const box = $("toasts");
    if (!box) return;
    while (box.childElementCount >= 3) box.removeChild(box.firstElementChild);
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 2600);
  }

  // ---- карточка задачи ----------------------------------------------------
  function taskSignature(t) {
    let tags = "";
    if (t.tags && t.tags.length) {
      for (let i = 0; i < t.tags.length; i++) tags += t.tags[i].name + ",";
    }
    return [
      t.title, t.done, t.due, t.scheduled, t.priority, t.important,
      t.project_id, t.archived, t.parent_id, tags,
    ].join("|");
  }

  function projectById(id) {
    for (let i = 0; i < state.projects.length; i++) {
      if (state.projects[i].id === id) return state.projects[i];
    }
    return null;
  }

  function buildTaskRow(entry) {
    const t = entry.task;
    const node = tplTask.content.firstElementChild.cloneNode(true);
    node.dataset.id = String(t.id);
    if (t.done) node.classList.add("done");
    if (t.parent_id) node.classList.add("sub");
    if (entry.isNew) node.classList.add("is-new");

    if (t.done) node.querySelector(".task-toggle").setAttribute("aria-label", "Снять отметку");
    text(node.querySelector(".task-title"), t.title);

    const meta = node.querySelector(".task-meta");
    const todayKey = today();
    if (t.due) {
      const span = document.createElement("span");
      if (t.due === todayKey) span.textContent = "сегодня";
      else if (t.due === shiftDays(1)) span.textContent = "завтра";
      else if (t.due === shiftDays(-1)) span.textContent = "вчера";
      else span.textContent = t.due.slice(8) + "." + t.due.slice(5, 7);
      if (t.due < todayKey && !t.done) span.className = "overdue";
      meta.appendChild(span);
    }
    if (t.scheduled) {
      const span = document.createElement("span");
      span.textContent = t.scheduled;
      meta.appendChild(span);
    }
    const project = t.project_id ? projectById(t.project_id) : null;
    if (project) {
      const span = document.createElement("span");
      span.textContent = (project.icon ? project.icon + " " : "") + project.name;
      meta.appendChild(span);
    }
    if (t.important) {
      const span = document.createElement("span");
      span.className = "task-prio";
      span.textContent = "!";
      meta.appendChild(span);
    }
    if (t.tags && t.tags.length) {
      for (let i = 0; i < t.tags.length; i++) {
        const pill = document.createElement("span");
        pill.className = "tag-pill";
        pill.textContent = t.tags[i].name;
        pill.style.background = t.tags[i].color || "#b44dff";
        meta.appendChild(pill);
      }
    }
    return node;
  }

  function taskEntries(list, newIds) {
    const entries = [];
    for (let i = 0; i < list.length; i++) {
      const task = list[i];
      entries.push({
        key: String(task.id),
        sig: taskSignature(task),
        task: task,
        isNew: Boolean(newIds && newIds[task.id]),
      });
    }
    return entries;
  }

  function visibleTasks() {
    const todayKey = today();
    const shown = [];
    for (let i = 0; i < state.tasks.length; i++) {
      const t = state.tasks[i];
      if (ui.archive ? !t.archived : t.archived) continue;
      if (ui.projectId && t.project_id !== ui.projectId) continue;
      if (ui.tview === "list") {
        if (ui.filter === "today" && (t.done || t.due !== todayKey)) continue;
        if (ui.filter === "upcoming" && (t.done || !t.due || t.due <= todayKey)) continue;
        if (ui.filter === "done" && !t.done) continue;
      }
      shown.push(t);
    }
    return sortTasks(shown);
  }

  function sortTasks(list) {
    return list.slice().sort((a, b) => {
      if (!!a.done !== !!b.done) return a.done ? 1 : -1;
      const prio = (b.priority || 0) - (a.priority || 0);
      if (prio) return prio;
      const dueA = a.due || "9999-99-99";
      const dueB = b.due || "9999-99-99";
      if (dueA !== dueB) return dueA < dueB ? -1 : 1;
      return a.id > b.id ? -1 : 1;
    });
  }

  // ---- шапка и храм -------------------------------------------------------
  function renderMasthead() {
    const p = state.profile;
    document.title = p.title;
    text($("mastheadTitle"), p.title);
    text($("mastheadSub"), p.sub);
    text($("templeName"), p.title.replace(/_/g, " "));
    text($("mastheadBadge"), demoMode ? "ДЕМО" : "RB3");
  }

  function renderTemple() {
    const strip = $("templeStats");
    const stats = state.stats;
    const cells = [
      [stats.open_today || 0, "сегодня"],
      [stats.done_today || 0, "выполнено"],
      [stats.pomo_minutes_today || 0, "мин фокуса"],
      [stats.done_total || 0, "всего"],
    ];
    const sig = cells.map((c) => c[0] + "/" + c[1]).join("|");
    if (strip.dataset.sig !== sig) {
      strip.dataset.sig = sig;
      resetContainer(strip);
      for (let i = 0; i < cells.length; i++) {
        const span = document.createElement("span");
        span.className = "stat";
        const b = document.createElement("b");
        b.textContent = cells[i][0];
        span.appendChild(b);
        span.appendChild(document.createTextNode(" " + cells[i][1]));
        strip.appendChild(span);
      }
    }

    const todayKey = today();
    const list = [];
    for (let i = 0; i < state.tasks.length; i++) {
      const t = state.tasks[i];
      if (t.archived || t.done) continue;
      if (t.due && t.due > todayKey) continue;
      list.push(t);
    }
    const sorted = sortTasks(list).slice(0, 8);
    patchChildren($("templeTasks"), taskEntries(sorted), buildTaskRow);
    $("templeEmpty").hidden = sorted.length > 0;

    const habits = state.habits.slice(0, 6);
    patchChildren($("templeHabits"), habits.map((h) => ({
      key: String(h.id),
      sig: h.name + "|" + h.days.join(","),
      habit: h,
    })), (entry) => {
      const node = document.createElement("div");
      node.className = "mini";
      const b = document.createElement("b");
      b.textContent = habitStreak(entry.habit);
      const span = document.createElement("span");
      span.textContent = entry.habit.name;
      node.appendChild(b);
      node.appendChild(span);
      return node;
    });
    $("templeHabitsEmpty").hidden = habits.length > 0;
  }

  function habitStreak(habit) {
    if (!habit.days || !habit.days.length) return 0;
    const set = new Set(habit.days);
    let streak = 0;
    let cursor = today();
    if (!set.has(cursor)) cursor = shiftDays(-1);
    while (set.has(cursor)) {
      streak++;
      cursor = shiftDays(-1, cursor);
    }
    return streak;
  }

  // ---- вид «Задачи» -------------------------------------------------------
  function syncChips(selector, key, value) {
    const nodes = document.querySelectorAll(selector);
    for (let i = 0; i < nodes.length; i++) {
      const active = nodes[i].dataset[key] === value;
      if (nodes[i].classList.contains("active") !== active) {
        nodes[i].classList.toggle("active", active);
      }
    }
  }

  // Чипы проектов пересобираются только при изменении набора проектов.
  function renderProjectChips() {
    const box = $("projectChips");
    const sig = state.projects.map((p) => p.id + ":" + p.icon + ":" + p.name).join("|") +
      "#" + ui.projectId;
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    resetContainer(box);
    const all = document.createElement("button");
    all.className = "chip" + (ui.projectId ? "" : " active");
    all.dataset.action = "project-pick";
    all.dataset.id = "";
    all.textContent = "Все";
    box.appendChild(all);
    for (let i = 0; i < state.projects.length; i++) {
      const project = state.projects[i];
      const chip = document.createElement("button");
      chip.className = "chip" + (ui.projectId === project.id ? " active" : "");
      chip.dataset.action = "project-pick";
      chip.dataset.id = String(project.id);
      chip.textContent = (project.icon ? project.icon + " " : "") + project.name;
      box.appendChild(chip);
    }
  }

  function renderTasks() {
    renderProjectChips();
    syncChips("[data-filter]", "filter", ui.filter);
    syncChips("[data-tview]", "tview", ui.tview);
    const archiveBtn = $("archiveBtn");
    archiveBtn.classList.toggle("active", ui.archive);
    archiveBtn.setAttribute("aria-pressed", ui.archive ? "true" : "false");

    const tasks = visibleTasks();
    const isList = ui.tview === "list";
    $("taskList").hidden = !isList;

    if (isList) {
      const roots = [];
      const children = [];
      for (let i = 0; i < tasks.length; i++) {
        if (tasks[i].parent_id) children.push(tasks[i]);
        else roots.push(tasks[i]);
      }
      const ordered = [];
      for (let i = 0; i < roots.length; i++) {
        ordered.push(roots[i]);
        for (let j = 0; j < children.length; j++) {
          if (children[j].parent_id === roots[i].id) ordered.push(children[j]);
        }
      }
      patchChildren($("taskList"), taskEntries(ordered), buildTaskRow);
    }
    $("taskEmpty").hidden = !(isList && !tasks.length);
    $("calWrap").hidden = ui.tview !== "cal";
    $("kanbanWrap").hidden = ui.tview !== "kanban";
    if (ui.tview === "cal") renderCalendar(tasks);
    else if (ui.tview === "kanban") renderKanban(tasks);
  }

  function monthKey(year, month, day) {
    return year + "-" + pad(month + 1) + "-" + pad(day);
  }

  function renderCalendar(tasks) {
    const now = new Date();
    if (ui.calY === null) {
      ui.calY = now.getFullYear();
      ui.calM = now.getMonth();
    }
    const label = new Date(ui.calY, ui.calM, 1)
      .toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
    text($("calLabel"), label);

    const byDay = {};
    for (let i = 0; i < tasks.length; i++) {
      const due = tasks[i].due;
      if (!due) continue;
      if (byDay[due]) byDay[due].push(tasks[i]);
      else byDay[due] = [tasks[i]];
    }

    const dayKeys = Object.keys(byDay).sort();
    let sig = ui.calY + "-" + ui.calM;
    for (let i = 0; i < dayKeys.length; i++) {
      sig += "," + dayKeys[i] + ":" + byDay[dayKeys[i]].length;
    }
    const grid = $("calGrid");
    if (grid.dataset.sig !== sig) {
      grid.dataset.sig = sig;
      buildCalendarGrid(grid, byDay);
    }

    const selected = grid.querySelector('.cal-day[data-day="' + ui.calSel + '"]');
    const current = grid.querySelector(".cal-day.sel");
    if (current && current !== selected) current.classList.remove("sel");
    if (selected) selected.classList.add("sel");
    text($("calDayLabel"), ui.calSel === today() ? "Сегодня" : ui.calSel);

    patchChildren(
      $("calDayList"),
      taskEntries(sortTasks(byDay[ui.calSel] || [])),
      buildTaskRow
    );
  }

  function buildCalendarGrid(grid, byDay) {
    resetContainer(grid);
    const dows = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    const frag = document.createDocumentFragment();
    for (let i = 0; i < 7; i++) {
      const head = document.createElement("div");
      head.className = "cal-dow";
      head.textContent = dows[i];
      frag.appendChild(head);
    }
    const first = (new Date(ui.calY, ui.calM, 1).getDay() || 7) - 1;
    const dim = new Date(ui.calY, ui.calM + 1, 0).getDate();
    const todayKey = today();
    for (let i = 0; i < first; i++) {
      const blank = document.createElement("div");
      blank.className = "cal-day blank";
      frag.appendChild(blank);
    }
    for (let day = 1; day <= dim; day++) {
      const key = monthKey(ui.calY, ui.calM, day);
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cal-day";
      cell.dataset.action = "cal-day";
      cell.dataset.day = key;
      cell.setAttribute("aria-label", key);
      if (key === todayKey) cell.classList.add("today");
      if (key === ui.calSel) cell.classList.add("sel");
      const label = document.createElement("span");
      label.textContent = day;
      cell.appendChild(label);
      const bucket = byDay[key];
      if (bucket) {
        const dot = document.createElement("span");
        dot.className = "cal-dot";
        dot.textContent = bucket.length > 9 ? "9+" : bucket.length;
        cell.appendChild(dot);
      }
      frag.appendChild(cell);
    }
    grid.appendChild(frag);
  }

  function renderKanban(tasks) {
    const todayKey = today();
    const columns = [
      { name: "Впереди", list: [] },
      { name: "Сегодня", list: [] },
      { name: "Готово", list: [] },
    ];
    for (let i = 0; i < tasks.length; i++) {
      const t = tasks[i];
      if (t.done) columns[2].list.push(t);
      else if (t.due && t.due <= todayKey) columns[1].list.push(t);
      else columns[0].list.push(t);
    }

    const box = $("kanbanCols");
    if (!box.dataset.built) {
      // Каркас колонок создаётся один раз, дальше обновляются только списки.
      box.dataset.built = "1";
      resetContainer(box);
      for (let i = 0; i < columns.length; i++) {
        const col = document.createElement("div");
        col.className = "kan-col";
        const head = document.createElement("div");
        head.className = "kan-head";
        const name = document.createElement("span");
        name.textContent = columns[i].name;
        const count = document.createElement("b");
        count.textContent = "0";
        head.appendChild(name);
        head.appendChild(count);
        const list = document.createElement("ul");
        list.className = "kan-list";
        col.appendChild(head);
        col.appendChild(list);
        box.appendChild(col);
      }
    }

    const cols = box.children;
    for (let i = 0; i < columns.length; i++) {
      const list = cols[i].querySelector(".kan-list");
      patchChildren(list, taskEntries(sortTasks(columns[i].list)), buildTaskRow);
      text(cols[i].querySelector(".kan-head b"), columns[i].list.length);
    }
  }

  // ---- привычки -----------------------------------------------------------
  function renderHabits() {
    const entries = [];
    for (let i = 0; i < state.habits.length; i++) {
      const habit = state.habits[i];
      entries.push({
        key: String(habit.id),
        sig: habit.name + "|" + habit.good + "|" + habit.color + "|" + habit.days.join(","),
        habit: habit,
      });
    }
    patchChildren($("habitList"), entries, buildHabitCard);
    $("habitEmpty").hidden = entries.length > 0;
  }

  function buildHabitCard(entry) {
    const habit = entry.habit;
    const node = tplHabit.content.firstElementChild.cloneNode(true);
    node.dataset.id = String(habit.id);
    text(node.querySelector(".habit-name"), (habit.good ? "+ " : "− ") + habit.name);
    text(node.querySelector(".habit-pct"), habitStreak(habit) + " дн. серия");

    const grid = node.querySelector(".habit-days");
    const set = new Set(habit.days || []);
    const frag = document.createDocumentFragment();
    for (let i = 27; i >= 0; i--) {
      const day = shiftDays(-i);
      const cell = document.createElement("div");
      cell.className = "habit-day";
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.action = "habit-day";
      button.dataset.id = String(habit.id);
      button.dataset.day = day;
      button.textContent = day.slice(8);
      button.setAttribute("aria-label", day);
      if (set.has(day)) button.classList.add("hit");
      button.style.setProperty("--c", habit.color || "#b44dff");
      cell.appendChild(button);
      frag.appendChild(cell);
    }
    grid.appendChild(frag);
    return node;
  }

  // ---- дневник ------------------------------------------------------------
  function renderDiary() {
    const entries = [];
    for (let i = 0; i < state.diary.length && i < 60; i++) {
      const item = state.diary[i];
      entries.push({
        key: item.day,
        sig: item.day + "|" + (item.mood || 0) + "|" + item.text.length,
        item: item,
      });
    }
    patchChildren($("diaryList"), entries, (entry) => {
      const node = document.createElement("div");
      node.className = "diary-entry";
      const date = document.createElement("div");
      date.className = "date";
      const moods = ["", "😞", "😐", "🙂", "🔥"];
      date.textContent = entry.item.day + (entry.item.mood ? "  " + moods[entry.item.mood] : "");
      const body = document.createElement("div");
      body.className = "text";
      body.textContent = entry.item.text;
      node.appendChild(date);
      node.appendChild(body);
      return node;
    });
    $("diaryEmpty").hidden = entries.length > 0;

    const draft = state.diary.filter((d) => d.day === today())[0];
    const input = $("diaryInput");
    if (draft && document.activeElement !== input && !input.value) input.value = draft.text;
  }

  // ---- статистика и помодоро ---------------------------------------------
  const statNodes = { values: [], labels: [] };

  function renderStats() {
    const stats = state.stats;
    const cards = [
      [stats.done_total || 0, "выполнено всего"],
      [stats.open_total || 0, "в работе"],
      [stats.done_today || 0, "сделано сегодня"],
      [stats.pomo_minutes_today || 0, "минут фокуса"],
    ];
    const grid = $("statsGrid");
    if (!grid.dataset.built) {
      grid.dataset.built = "1";
      resetContainer(grid);
      statNodes.values.length = 0;
      statNodes.labels.length = 0;
      for (let i = 0; i < cards.length; i++) {
        const card = document.createElement("div");
        card.className = "stat-card";
        const value = document.createElement("b");
        const label = document.createElement("span");
        label.textContent = cards[i][1];
        card.appendChild(value);
        card.appendChild(label);
        grid.appendChild(card);
        statNodes.values.push(value);
        statNodes.labels.push(label);
      }
    }
    for (let i = 0; i < cards.length; i++) text(statNodes.values[i], cards[i][0]);
    renderPomo();
  }

  function renderPomo() {
    text($("pomoDisplay"), fmtTime(pomoLeft()));
    const toggle = $("pomoToggle");
    text(toggle, ui.pomo.running ? "Стоп" : "Старт");
    const panel = document.querySelector(".pomo-panel");
    if (panel) panel.classList.toggle("pomo-running", ui.pomo.running);
    syncChips("[data-min]", "min", String(ui.pomo.minutes));
  }

  function pomoLeft() {
    if (!ui.pomo.running) return ui.pomo.minutes * 60;
    return Math.max(0, Math.round((ui.pomo.endAt - Date.now()) / 1000));
  }

  function fmtTime(seconds) {
    return pad(Math.floor(seconds / 60)) + ":" + pad(seconds % 60);
  }

  // ---- настройки (культ) --------------------------------------------------
  function renderCult() {
    const p = state.profile;
    setInput("setTitle", p.title);
    setInput("setSub", p.sub);
    setInput("setLabel", p.label);
    setInput("setAccent", p.accent);
    syncChips("[data-mode]", "mode", p.mode);
    renderProjects();
  }

  // Поле не перезаписываем, пока пользователь в нём печатает.
  function setInput(id, value) {
    const el = $(id);
    if (!el || document.activeElement === el) return;
    if (el.value !== value) el.value = value;
  }

  function applyProfile() {
    const p = state.profile;
    document.documentElement.style.setProperty("--accent", p.accent);
    if (document.body.dataset.mode !== p.mode) document.body.dataset.mode = p.mode;
    dirty.masthead = true;
    dirty.cult = true;
    dirty[ui.view] = true;
    scheduleRender();
  }

  // ---- действия над задачами ---------------------------------------------
  const newIds = {};

  function recountStats() {
    const todayKey = today();
    let doneTotal = 0, openTotal = 0, doneToday = 0, openToday = 0, overdue = 0;
    for (let i = 0; i < state.tasks.length; i++) {
      const t = state.tasks[i];
      if (t.archived) continue;
      if (t.done) {
        doneTotal++;
        if ((t.updated || "").slice(0, 10) === todayKey) doneToday++;
      } else {
        openTotal++;
        if (t.due === todayKey) openToday++;
        else if (t.due && t.due < todayKey) overdue++;
      }
    }
    state.stats.done_total = doneTotal;
    state.stats.open_total = openTotal;
    state.stats.done_today = doneToday;
    state.stats.open_today = openToday;
    state.stats.overdue = overdue;
    state.stats.habit_count = state.habits.length;
  }

  const WEEKDAYS = {
    "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7,
    "понедельник": 1, "вторник": 2, "среда": 3, "четверг": 4,
    "пятница": 5, "суббота": 6, "воскресенье": 7,
  };

  /** Разбирает «завтра 09:00 обряд #ритуал !важно» в поля задачи. */
  function parseSmart(raw) {
    const lower = " " + raw.toLowerCase() + " ";
    const parsed = { due: null, scheduled: null, important: false, tagIds: [], tags: [] };

    const time = lower.match(/(\d{1,2}):(\d{2})/);
    if (time) {
      const hour = parseInt(time[1], 10);
      const minute = parseInt(time[2], 10);
      if (hour < 24 && minute < 60) parsed.scheduled = pad(hour) + ":" + pad(minute);
    }

    if (lower.indexOf("послезавтра") !== -1) parsed.due = shiftDays(2);
    else if (lower.indexOf("завтра") !== -1) parsed.due = shiftDays(1);
    else if (lower.indexOf("сегодня") !== -1) parsed.due = today();
    else {
      const nowDow = new Date().getDay() || 7;
      for (const word in WEEKDAYS) {
        if (lower.indexOf(word) !== -1) {
          let diff = WEEKDAYS[word] - nowDow;
          if (diff <= 0) diff += 7;
          parsed.due = shiftDays(diff);
          break;
        }
      }
    }

    const date = lower.match(/(\d{1,2})[.\/](\d{1,2})(?:[.\/](\d{2,4}))?/);
    if (date) {
      let year = date[3] ? parseInt(date[3], 10) : new Date().getFullYear();
      if (year < 100) year += 2000;
      parsed.due = year + "-" + pad(parseInt(date[2], 10)) + "-" + pad(parseInt(date[1], 10));
    }

    const tagWords = raw.match(/#[^\s#]+/g) || [];
    for (let i = 0; i < tagWords.length; i++) {
      const name = tagWords[i].slice(1).trim().toLowerCase();
      if (!name) continue;
      const tag = ensureTag(name);
      if (parsed.tagIds.indexOf(tag.id) === -1) {
        parsed.tagIds.push(tag.id);
        parsed.tags.push(tag);
      }
    }

    parsed.important = /(^|\s)!/.test(raw);
    return parsed;
  }

  function smartTitle(raw) {
    return raw
      .replace(/#[^\s#]+/g, " ")
      .replace(/\b(сегодня|завтра|послезавтра)\b/gi, " ")
      .replace(/\b(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b/gi, " ")
      .replace(/(^|\s)(пн|вт|ср|чт|пт|сб|вс)(\s|$)/gi, " ")
      .replace(/\b\d{1,2}:\d{2}\b/g, " ")
      .replace(/\b\d{1,2}[.\/]\d{1,2}(?:[.\/]\d{2,4})?\b/g, " ")
      .replace(/(^|\s)!+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  // Тег создаётся локально и тут же уезжает на сервер отдельной операцией.
  function ensureTag(name) {
    for (let i = 0; i < state.tags.length; i++) {
      if (String(state.tags[i].name).toLowerCase() === name) return state.tags[i];
    }
    const tempId = "tag" + uid();
    const tag = { id: tempId, name: name, color: tagColor(state.tags.length) };
    state.tags.push(tag);
    queueOps({ t: "tag.add", cid: tempId, data: { name: name, color: tag.color } });
    return tag;
  }

  const TAG_COLORS = ["#b44dff", "#27c8ff", "#38d96a", "#ffb020", "#ff5c8a", "#8f7bff"];

  function tagColor(index) {
    return TAG_COLORS[index % TAG_COLORS.length];
  }

  function addTaskFromInput(raw) {
    const parsed = parseSmart(raw);
    const title = smartTitle(raw) || raw.trim();
    if (!title) return;

    const cid = uid();
    const stamp = new Date().toISOString().slice(0, 19);
    const task = {
      id: "tmp" + uid(),
      parent_id: null,
      project_id: ui.projectId,
      title: title,
      note: "",
      done: 0,
      archived: 0,
      priority: 0,
      important: parsed.important ? 1 : 0,
      due: parsed.due,
      scheduled: parsed.scheduled,
      repeat: "",
      position: 0,
      created: stamp,
      updated: stamp,
      tags: parsed.tags,
      _cid: cid,
    };
    tempIds[cid] = task.id;
    state.tasks.push(task);
    newIds[task.id] = true;

    queueOps({
      t: "task.add",
      cid: cid,
      data: {
        title: task.title, due: task.due, scheduled: task.scheduled,
        important: task.important, project_id: task.project_id,
        parent_id: null, tags: parsed.tagIds,
      },
    });

    recountStats();
    markAllDirty();
    toast("Записано", "success");
    setTimeout(() => { delete newIds[task.id]; }, 400);
  }

  function toggleTask(id) {
    const task = findTask(id);
    if (!task) return;
    task.done = task.done ? 0 : 1;
    task.updated = new Date().toISOString().slice(0, 19);
    queueTaskPatch(task, { done: task.done });
    recountStats();
    markAllDirty();
  }

  function deleteTask(id) {
    const task = findTask(id);
    if (!task) return;
    if (task._cid) {
      // Задача ещё не улетела на сервер — достаточно убрать её из очереди.
      for (let i = 0; i < queue.ops.length; i++) {
        if (queue.ops[i].t === "task.add" && queue.ops[i].cid === task._cid) {
          queue.ops.splice(i, 1);
          break;
        }
      }
      delete tempIds[task._cid];
      persistQueue();
    } else {
      queueOps({ t: "task.delete", id: id });
    }
    state.tasks = state.tasks.filter((t) => t.id !== id);
    recountStats();
    markAllDirty();
    toast("Удалено");
  }

  function clearDone() {
    let moved = 0;
    for (let i = 0; i < state.tasks.length; i++) {
      const task = state.tasks[i];
      if (task.done && !task.archived) {
        task.archived = 1;
        queueTaskPatch(task, { archived: 1 });
        moved++;
      }
    }
    recountStats();
    markAllDirty();
    toast(moved ? "Выполненные — в архив" : "Нечего архивировать");
  }

  // ---- модальное окно задачи ---------------------------------------------
  function openModal(id) {
    const task = findTask(id);
    if (!task) return;
    ui.modalTaskId = id;
    fillModal(task);
    $("modal").hidden = false;
    if (tg && tg.BackButton) {
      try { tg.BackButton.show(); } catch (e) {}
    }
  }

  function closeModal() {
    ui.modalTaskId = null;
    $("modal").hidden = true;
    if (tg && tg.BackButton) {
      try { tg.BackButton.hide(); } catch (e) {}
    }
  }

  function fillModal(task) {
    text($("modalTitle"), task.title);
    setInput("mTitle", task.title);
    setInput("mDue", task.due || "");
    setInput("mTime", task.scheduled || "");
    setInput("mNote", task.note || "");
    const select = $("mProject");
    const sig = state.projects.map((p) => p.id).join(",");
    if (select.dataset.sig !== sig) {
      select.dataset.sig = sig;
      select.textContent = "";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "Без проекта";
      select.appendChild(none);
      for (let i = 0; i < state.projects.length; i++) {
        const option = document.createElement("option");
        option.value = String(state.projects[i].id);
        option.textContent = (state.projects[i].icon ? state.projects[i].icon + " " : "") +
          state.projects[i].name;
        select.appendChild(option);
      }
    }
    select.value = task.project_id ? String(task.project_id) : "";
    text(document.querySelector('[data-action="modal-priority"]'), "Приоритет: " + (task.priority || 0));
    text(document.querySelector('[data-action="modal-important"]'),
      task.important ? "Важно: да" : "Важно: нет");
    text(document.querySelector('[data-action="modal-done"]'),
      task.done ? "Вернуть в работу" : "Выполнено");
    text(document.querySelector('[data-action="modal-archive"]'),
      task.archived ? "Из архива" : "В архив");
  }

  function modalTask() {
    return ui.modalTaskId ? findTask(ui.modalTaskId) : null;
  }

  function patchModalTask(patch, keepOpen) {
    const task = modalTask();
    if (!task) return;
    Object.assign(task, patch);
    if (!Object.prototype.hasOwnProperty.call(patch, "updated")) {
      task.updated = new Date().toISOString().slice(0, 19);
    }
    queueTaskPatch(task, patch);
    recountStats();
    markAllDirty();
    if (keepOpen !== false) fillModal(task);
  }

  // ---- привычки -----------------------------------------------------------
  function addHabit(name) {
    const good = $("habitGood").checked ? 1 : 0;
    const cid = uid();
    const habit = {
      id: "tmp" + uid(),
      name: name,
      good: good,
      color: tagColor(state.habits.length + 2),
      days: [],
      _cid: cid,
    };
    tempIds[cid] = habit.id;
    state.habits.push(habit);
    queueOps({
      t: "habit.add",
      cid: cid,
      data: { name: name, good: good, color: habit.color, days: [] },
    });
    recountStats();
    markAllDirty();
    toast("Привычка добавлена", "success");
  }

  function replaceHabitById(tempId, item) {
    if (tempId == null) {
      upsertHabit(item);
      return;
    }
    for (let i = 0; i < state.habits.length; i++) {
      if (state.habits[i].id === tempId) {
        state.habits[i] = item;
        return;
      }
    }
    state.habits.push(item);
  }

  function toggleHabitDay(id, day) {
    const habit = findHabit(id);
    if (!habit) return;
    const set = new Set(habit.days || []);
    const turningOn = !set.has(day);
    if (turningOn) set.add(day);
    else set.delete(day);
    habit.days = Array.from(set).sort();

    if (habit._cid) {
      // Создание привычки ещё в очереди — дописываем дни в ту же операцию.
      for (let i = 0; i < queue.ops.length; i++) {
        const op = queue.ops[i];
        if (op.t === "habit.add" && op.cid === habit._cid) {
          op.data.days = habit.days.slice();
          break;
        }
      }
      persistQueue();
      scheduleFlush();
    } else {
      queueOps({ t: "habit.toggle", id: id, day: day });
    }
    markDirty("habits");
    markDirty("temple");
  }

  function deleteHabit(id) {
    const habit = findHabit(id);
    if (!habit) return;
    if (habit._cid) {
      for (let i = 0; i < queue.ops.length; i++) {
        if (queue.ops[i].t === "habit.add" && queue.ops[i].cid === habit._cid) {
          queue.ops.splice(i, 1);
          break;
        }
      }
      delete tempIds[habit._cid];
      persistQueue();
    } else {
      queueOps({ t: "habit.delete", id: id });
    }
    state.habits = state.habits.filter((h) => h.id !== id);
    recountStats();
    markAllDirty();
  }

  // ---- дневник -----------------------------------------------------------
  function saveDiaryEntry() {
    const input = $("diaryInput");
    const body = input.value.trim();
    if (!body) {
      toast("Пустая запись", "error");
      return;
    }
    const day = today();
    const entry = { id: 0, day: day, text: body, mood: ui.mood, updated: new Date().toISOString().slice(0, 19) };
    upsertDiary(entry);
    queueOps({ t: "diary.save", data: { day: day, text: body, mood: ui.mood } });
    input.value = "";
    setMood(null);
    markDirty("diary");
    toast("Сохранено", "success");
  }

  function setMood(mood) {
    ui.mood = mood;
    syncChips("[data-mood]", "mood", mood == null ? "" : String(mood));
  }

  // ---- помодоро ----------------------------------------------------------
  let pomoTimer = null;

  function setPomoMinutes(minutes) {
    if (ui.pomo.running) return; // не дергаем интервал во время фокуса
    ui.pomo.minutes = minutes;
    ui.pomo.endAt = 0;
    renderPomo();
  }

  function togglePomo() {
    if (ui.pomo.running) stopPomo(false);
    else startPomo();
  }

  function startPomo() {
    ui.pomo.running = true;
    ui.pomo.endAt = Date.now() + ui.pomo.minutes * 60000;
    if (pomoTimer) clearInterval(pomoTimer);
    // Отсчёт по метке времени: интервал не «плывёт» при просадках fps.
    pomoTimer = setInterval(pomoTick, 500);
    renderPomo();
  }

  function stopPomo(finished) {
    ui.pomo.running = false;
    if (pomoTimer) {
      clearInterval(pomoTimer);
      pomoTimer = null;
    }
    if (finished) {
      queueOps({ t: "pomo.add", data: { minutes: ui.pomo.minutes, completed: true } });
      state.stats.pomo_minutes_today = (state.stats.pomo_minutes_today || 0) + ui.pomo.minutes;
      state.stats.pomo_count_today = (state.stats.pomo_count_today || 0) + 1;
      toast("Фокус завершён: +" + ui.pomo.minutes + " мин", "success");
      markAllDirty();
    }
    renderPomo();
  }

  function pomoTick() {
    if (!ui.pomo.running) return;
    const left = pomoLeft();
    if (ui.view === "stats") text($("pomoDisplay"), fmtTime(left));
    if (left <= 0) stopPomo(true);
  }

  // ---- профиль -----------------------------------------------------------
  let profileTimer = null;

  function saveProfileSoon() {
    if (profileTimer) clearTimeout(profileTimer);
    profileTimer = setTimeout(() => {
      profileTimer = null;
      queue.profile = Object.assign({}, state.profile);
      persistQueue();
      scheduleFlush();
    }, 600);
  }

  function resetProfile() {
    state.profile = Object.assign({}, DEFAULT_PROFILE);
    applyProfile();
    renderCult();
    saveProfileSoon();
    toast("Оформление сброшено");
  }

  // ---- проекты ------------------------------------------------------------
  function replaceProjectById(tempId, item) {
    if (tempId != null) {
      for (let i = 0; i < state.projects.length; i++) {
        if (state.projects[i].id === tempId) {
          state.projects[i] = item;
          return;
        }
      }
    }
    upsertById(state.projects, item);
  }

  function addProject(name, icon) {
    const cid = uid();
    const project = { id: "tmp" + uid(), name: name, icon: icon || "", color: "#ff0d0d", sort: 0 };
    tempIds[cid] = project.id;
    state.projects.push(project);
    queueOps({ t: "project.add", cid: cid, data: { name: name, icon: icon || "" } });
    markAllDirty();
    toast("Проект создан", "success");
  }

  function deleteProject(id) {
    queueOps({ t: "project.delete", id: parseInt(id, 10) || id });
    state.projects = state.projects.filter((p) => String(p.id) !== String(id));
    for (let i = 0; i < state.tasks.length; i++) {
      if (String(state.tasks[i].project_id) === String(id)) state.tasks[i].project_id = null;
    }
    if (ui.projectId != null && String(ui.projectId) === String(id)) ui.projectId = null;
    markAllDirty();
    toast("Проект удалён");
  }

  function buildProjectRow(entry) {
    const project = entry.project;
    const node = document.createElement("div");
    node.className = "project-row";
    node.dataset.id = String(project.id);
    const icon = document.createElement("span");
    icon.className = "project-icon";
    icon.textContent = project.icon || "◆";
    const name = document.createElement("span");
    name.className = "project-name";
    name.textContent = project.name;
    const del = document.createElement("button");
    del.className = "task-del";
    del.dataset.action = "project-del";
    del.setAttribute("aria-label", "Удалить проект");
    del.textContent = "✕";
    node.appendChild(icon);
    node.appendChild(name);
    node.appendChild(del);
    return node;
  }

  function renderProjects() {
    const box = $("projectList");
    const entries = state.projects.map((p) => ({
      key: String(p.id),
      sig: p.name + "|" + p.icon + "|" + p.color,
      project: p,
    }));
    patchChildren(box, entries, buildProjectRow);
    $("projectEmpty").hidden = entries.length > 0;
  }

  // ---- навигация ----------------------------------------------------------
  function switchView(name) {
    if (VIEWS.indexOf(name) === -1) return;
    ui.view = name;
    const sections = document.querySelectorAll(".view");
    for (let i = 0; i < sections.length; i++) {
      const show = sections[i].id === "view-" + name;
      if (sections[i].hidden === show) sections[i].hidden = !show;
    }
    syncChips(".tab", "view", name);
    dirty[name] = true;
    render();
    scrollTop();
  }

  // window.scrollTo есть не во всех webview: молча пропускаем.
  function scrollTop() {
    try {
      const scroller = window.scrollTo;
      if (typeof scroller !== "function") return;
      scroller.call(window, 0, 0);
    } catch (e) {}
  }

  function shiftMonth(delta) {
    if (ui.calY === null) {
      const now = new Date();
      ui.calY = now.getFullYear();
      ui.calM = now.getMonth();
    }
    if (delta < 0) {
      if (ui.calM === 0) { ui.calM = 11; ui.calY--; }
      else ui.calM--;
    } else {
      if (ui.calM === 11) { ui.calM = 0; ui.calY++; }
      else ui.calM++;
    }
    markDirty("tasks");
  }

  function addSubtask() {
    const parent = modalTask();
    if (!parent) return;
    if (parent._cid) {
      toast("Задача ещё синхронизируется", "error");
      return;
    }
    const cid = uid();
    const stamp = new Date().toISOString().slice(0, 19);
    const sub = {
      id: "tmp" + uid(),
      parent_id: parent.id,
      project_id: parent.project_id,
      title: "Подзадача",
      note: "",
      done: 0,
      archived: 0,
      priority: 0,
      important: 0,
      due: null,
      scheduled: null,
      repeat: "",
      position: 0,
      created: stamp,
      updated: stamp,
      tags: [],
      _cid: cid,
    };
    tempIds[cid] = sub.id;
    state.tasks.push(sub);
    queueOps({
      t: "task.add",
      cid: cid,
      data: { title: sub.title, parent_id: parent.id, project_id: parent.project_id },
    });
    recountStats();
    closeModal();
    markAllDirty();
    toast("Подзадача добавлена", "success");
  }

  // ---- события ------------------------------------------------------------
  function rowIdOf(el) {
    const holder = el.closest("[data-id]");
    return holder ? holder.dataset.id : null;
  }

  function handleAction(action, el) {
    if (action === "task-open") {
      const id = rowIdOf(el);
      if (id != null) openModal(id);
      return;
    }
    if (action === "task-toggle") {
      const id = rowIdOf(el);
      if (id != null) toggleTask(id);
      return;
    }
    if (action === "task-del") {
      const id = rowIdOf(el);
      if (id != null) deleteTask(id);
      return;
    }
    if (action === "habit-day") {
      toggleHabitDay(el.dataset.id, el.dataset.day);
      return;
    }
    if (action === "habit-del") {
      const id = rowIdOf(el);
      if (id != null) deleteHabit(id);
      return;
    }
    if (action === "project-pick") {
      ui.projectId = el.dataset.id ? parseInt(el.dataset.id, 10) : null;
      markDirty("tasks");
      return;
    }
    if (action === "project-del") {
      const id = rowIdOf(el);
      if (id != null) deleteProject(id);
      return;
    }
    if (action === "cal-day") {
      ui.calSel = el.dataset.day;
      markDirty("tasks");
      return;
    }
    if (action === "modal-close") { closeModal(); return; }
    if (action === "modal-subtask") { addSubtask(); return; }
    if (action === "modal-priority") {
      const t = modalTask();
      if (t) patchModalTask({ priority: ((t.priority || 0) + 1) % 4 });
      return;
    }
    if (action === "modal-important") {
      const t = modalTask();
      if (t) patchModalTask({ important: t.important ? 0 : 1 });
      return;
    }
    if (action === "modal-done") {
      const t = modalTask();
      if (t) patchModalTask({ done: t.done ? 0 : 1 });
      return;
    }
    if (action === "modal-archive") {
      const t = modalTask();
      if (t) patchModalTask({ archived: t.archived ? 0 : 1 });
      closeModal();
      return;
    }
    if (action === "modal-delete") {
      const t = modalTask();
      closeModal();
      if (t) deleteTask(t.id);
    }
  }

  function onDocumentClick(event) {
    const actionEl = event.target.closest ? event.target.closest("[data-action]") : null;
    if (actionEl && document.body.contains(actionEl)) {
      handleAction(actionEl.dataset.action, actionEl);
      return;
    }
    const chip = event.target.closest
      ? event.target.closest("[data-view],[data-tview],[data-filter],[data-mode],[data-min],[data-mood]")
      : null;
    if (!chip || !document.body.contains(chip)) return;
    const data = chip.dataset;
    if (data.view) switchView(data.view);
    else if (data.tview) { ui.tview = data.tview; markDirty("tasks"); }
    else if (data.filter) { ui.filter = data.filter; markDirty("tasks"); }
    else if (data.mode) { state.profile.mode = data.mode; applyProfile(); saveProfileSoon(); }
    else if (data.min) setPomoMinutes(parseInt(data.min, 10));
    else if (data.mood) {
      const mood = parseInt(data.mood, 10);
      setMood(ui.mood === mood ? null : mood);
    }
  }

  // ---- привязка событий ---------------------------------------------------
  function onChange(id, handler) {
    const el = $(id);
    if (el) el.addEventListener("change", () => handler(el.value));
  }

  function submitter(inputId) {
    return (event) => {
      event.preventDefault();
      const input = $(inputId);
      const value = input.value.trim();
      if (value) {
        addTaskFromInput(value);
        input.value = "";
      }
      input.blur(); // спрятать клавиатуру на телефоне
    };
  }

  function bindAll() {
    document.addEventListener("click", onDocumentClick, { passive: true });

    $("templeQuickForm").addEventListener("submit", submitter("templeQuickInput"));
    $("taskAddForm").addEventListener("submit", submitter("taskAddInput"));
    $("habitForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("habitInput");
      const value = input.value.trim();
      if (value) {
        addHabit(value);
        input.value = "";
      }
      input.blur();
    });

    $("archiveBtn").addEventListener("click", () => {
      ui.archive = !ui.archive;
      markDirty("tasks");
    });
    $("calPrev").addEventListener("click", () => shiftMonth(-1));
    $("calNext").addEventListener("click", () => shiftMonth(1));
    $("clearDone").addEventListener("click", clearDone);
    $("diarySave").addEventListener("click", saveDiaryEntry);
    $("pomoToggle").addEventListener("click", togglePomo);
    $("resetBtn").addEventListener("click", resetProfile);
    $("projectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const nameInput = $("projectInput");
      const iconInput = $("projectIcon");
      const name = nameInput.value.trim();
      if (!name) return;
      addProject(name, iconInput.value.trim());
      nameInput.value = "";
      iconInput.value = "";
      nameInput.blur();
    });

    $("setTitle").addEventListener("input", (event) => {
      state.profile.title = event.target.value || DEFAULT_PROFILE.title;
      applyProfile();
      saveProfileSoon();
    });
    $("setSub").addEventListener("input", (event) => {
      state.profile.sub = event.target.value;
      applyProfile();
      saveProfileSoon();
    });
    $("setLabel").addEventListener("input", (event) => {
      state.profile.label = event.target.value.trim() || DEFAULT_PROFILE.label;
      saveProfileSoon();
    });
    $("setAccent").addEventListener("input", (event) => {
      state.profile.accent = event.target.value;
      document.documentElement.style.setProperty("--accent", state.profile.accent);
      saveProfileSoon();
    });

    onChange("mTitle", (value) => {
      const task = modalTask();
      if (task && value.trim()) patchModalTask({ title: value.trim() });
    });
    onChange("mProject", (value) => {
      patchModalTask({ project_id: value ? parseInt(value, 10) : null });
    });
    onChange("mDue", (value) => patchModalTask({ due: value || null }));
    onChange("mTime", (value) => patchModalTask({ scheduled: value || null }));
    onChange("mNote", (value) => patchModalTask({ note: value }));

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("modal").hidden) closeModal();
    });
    $("modal").addEventListener("click", (event) => {
      if (event.target === $("modal")) closeModal();
    });
  }

  // ---- Telegram -----------------------------------------------------------
  function initTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      if (tg.expand) tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#0a0608");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#0a0608");
      // Иначе вертикальный свайп по контенту может случайно закрыть приложение.
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      if (tg.BackButton && tg.BackButton.onClick) {
        tg.BackButton.onClick(() => {
          if (!$("modal").hidden) closeModal();
        });
      }
    } catch (e) { /* не критично */ }
  }

  // ---- запуск -------------------------------------------------------------
  function activateAsyncCss() {
    const link = document.querySelector("link[data-async-css]");
    if (!link) return;
    const apply = () => { link.media = "all"; };
    if (link.sheet) apply();
    else link.addEventListener("load", apply, { once: true });
    setTimeout(apply, 4000); // страховка на случай кеша/блокировщика
  }

  function showBootFail(message) {
    const el = $("bootFail");
    if (!el) return;
    if (message) el.textContent = message;
    el.hidden = false;
  }

  function boot() {
    try {
      activateAsyncCss();
      if (!authReady) {
        showBootFail("Откройте дневник через Telegram — по кнопке в боте.");
        return;
      }
      restore();
      initTelegram();
      bindAll();
      applyProfile();
      recountStats();
      render();
      window.__booted = true;
      syncNow();
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) syncNow();
      });
      window.addEventListener("pagehide", () => {
        persistQueue();
        persistCache();
      });
      // Лёгкий фоновый пульс: если сервер «уснул» или пользователь вернулся
      // позже, состояние подтягивается само (условный запрос почти бесплатен).
      setInterval(() => {
        if (!syncing && !document.hidden) syncNow();
      }, 60000);
    } catch (e) {
      showBootFail("Не запустилось. Обновите Telegram и откройте заново.");
      throw e;
    }
  }

  // Страховка: если до boot дойти не удалось (например, упал парсер),
  // через 10 секунд пользователь увидит понятное сообщение.
  setTimeout(() => {
    if (!window.__booted) showBootFail();
  }, 10000);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
