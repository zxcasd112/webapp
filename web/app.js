(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;

  const $ = (id) => document.getElementById(id);

  const DEFAULT = {
    title: "slaughter_lord",
    sub: "Записывай. Кастомизируй. Побеждай.",
    label: "задача",
    accent: "#ff0d0d",
    mode: "blood",
    tasks: [],
  };

  let state = { ...DEFAULT, tasks: [] };
  let filter = "all";
  let ready = false;
  let demo = false;
  let saving = false;
  let savePending = false;

  const initData = tg ? tg.initData : "";

  function apiPath() {
    return `/api/state?initData=${encodeURIComponent(initData)}`;
  }

  function loadLocal() {
    try {
      const raw = localStorage.getItem("taskjournal");
      if (raw) state = { ...DEFAULT, ...JSON.parse(raw) };
    } catch (e) {
      state = { ...DEFAULT, tasks: [] };
    }
    if (!Array.isArray(state.tasks)) state.tasks = [];
  }

  async function fetchState() {
    if (!initData) {
      // открыто в обычном браузере — демо-режим, данные локально
      demo = true;
      loadLocal();
      ready = true;
      return;
    }
    const res = await fetch(apiPath());
    if (!res.ok) throw new Error("load failed " + res.status);
    const data = await res.json();
    state = { ...DEFAULT, ...data };
    if (!Array.isArray(state.tasks)) state.tasks = [];
    ready = true;
  }

  async function persist() {
    if (demo) {
      try {
        localStorage.setItem("taskjournal", JSON.stringify(state));
      } catch (e) {
        /* нечего делать */
      }
      return;
    }
    if (saving) {
      savePending = true;
      return;
    }
    saving = true;
    try {
      await fetch(apiPath(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
      });
    } catch (e) {
      /* показывать нечего — повторим при следующем изменении */
    } finally {
      saving = false;
      if (savePending) {
        savePending = false;
        persist();
      }
    }
  }

  function save() {
    if (!ready) return;
    persist();
  }

  function plural(n) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return `${n} ${state.label}`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14))
      return `${n} ${state.label}и`;
    return `${n} ${state.label}`;
  }

  function renderMeta() {
    const active = state.tasks.filter((t) => !t.done).length;
    const done = state.tasks.length - active;
    $("taskCount").textContent = plural(state.tasks.length);
    $("doneCount").textContent = `${done} выполнено`;
  }

  function render() {
    $("appTitle").textContent = state.title;
    $("appSub").textContent = state.sub;
    $("setTitle").value = state.title;
    $("setSub").value = state.sub;
    $("setLabel").value = state.label;
    $("editionBadge").textContent = demo ? "DEMO" : "RB3";

    document.documentElement.style.setProperty("--accent", state.accent);
    document.body.dataset.mode = state.mode;
    document
      .querySelectorAll("[data-mode]")
      .forEach((b) => b.classList.toggle("active", b.dataset.mode === state.mode));

    renderMeta();

    const list = $("taskList");
    list.innerHTML = "";

    const shown = state.tasks.filter((t) => {
      if (filter === "active") return !t.done;
      if (filter === "done") return t.done;
      return true;
    });

    $("emptyState").style.display = shown.length ? "none" : "block";

    shown.forEach((task) => {
      const li = document.createElement("li");
      li.className = "task" + (task.done ? " done" : "");

      const toggle = document.createElement("button");
      toggle.className = "task-toggle";
      toggle.textContent = "\u2713";
      toggle.addEventListener("click", () => toggleTask(task.id));

      const text = document.createElement("span");
      text.className = "task-text";
      text.textContent = task.text;

      const date = document.createElement("span");
      date.className = "task-date";
      date.textContent = task.date || "";

      const del = document.createElement("button");
      del.className = "task-del";
      del.textContent = "\u2715";
      del.addEventListener("click", () => deleteTask(task.id));

      li.append(toggle, text, date, del);
      list.appendChild(li);
    });
  }

  function addTask(text) {
    state.tasks.unshift({
      id: Date.now() + Math.random(),
      text,
      done: false,
      date: new Date().toLocaleDateString("ru-RU"),
    });
    save();
    render();
  }

  function toggleTask(id) {
    const t = state.tasks.find((x) => x.id === id);
    if (t) {
      t.done = !t.done;
      save();
      render();
    }
  }

  function deleteTask(id) {
    state.tasks = state.tasks.filter((x) => x.id !== id);
    save();
    render();
  }

  function clearDone() {
    state.tasks = state.tasks.filter((t) => !t.done);
    save();
    render();
  }

  function reset() {
    state = { ...DEFAULT, tasks: [] };
    save();
    render();
  }

  function bindEvents() {
    $("addForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const val = $("taskInput").value.trim();
      if (val) addTask(val);
      $("taskInput").value = "";
    });

    document.querySelectorAll("[data-filter]").forEach((b) => {
      b.addEventListener("click", () => {
        filter = b.dataset.filter;
        document
          .querySelectorAll("[data-filter]")
          .forEach((x) => x.classList.toggle("active", x === b));
        render();
      });
    });

    document.querySelectorAll("[data-mode]").forEach((b) => {
      b.addEventListener("click", () => {
        state.mode = b.dataset.mode;
        save();
        render();
      });
    });

    $("setTitle").addEventListener("input", (e) => {
      state.title = e.target.value || DEFAULT.title;
      $("appTitle").textContent = state.title;
      save();
    });

    $("setSub").addEventListener("input", (e) => {
      state.sub = e.target.value;
      $("appSub").textContent = state.sub;
      save();
    });

    $("setLabel").addEventListener("input", (e) => {
      state.label = e.target.value.trim() || DEFAULT.label;
      renderMeta();
      save();
    });

    $("clearDone").addEventListener("click", clearDone);
    $("resetBtn").addEventListener("click", reset);
  }

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.setHeaderColor("#070203");
      tg.setBackgroundColor("#070203");
    }

    bindEvents();

    try {
      await fetchState();
    } catch (e) {
      // сервер недоступен (сон бесплатного тарифа, сеть) — работаем локально
      demo = true;
      loadLocal();
      ready = true;
    }
    render();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
