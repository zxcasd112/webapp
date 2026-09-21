/* End-to-end проверка фронтенда: app.js выполняется в jsdom и ходит
 * по сети в настоящий Python-сервер (стартующий подпроцессом).
 *
 * Запуск:  node tools/e2e.js
 * Нужен jsdom: npm install jsdom@24  (кладём в %TEMP%\jstest)
 */

"use strict";

const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..");

// jsdom ищем во временной папке, чтобы не тащить node_modules в проект
const { JSDOM } = require(path.join(os.tmpdir(), "jstest", "node_modules", "jsdom"));

let BASE = "";

const results = [];
function check(name, cond) {
  results.push(Boolean(cond));
  console.log((cond ? "ok   " : "FAIL ") + name);
  if (!cond) process.exitCode = 1;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function startServer(port) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tj-e2e-"));
  const env = Object.assign({}, process.env, {
    BOT_TOKEN: "",
    DEV_MODE: "1",
    WEB_PORT: String(port),
    DATA_DIR: tmp,
    PYTHONUNBUFFERED: "1",
  });
  return spawn("python", [path.join(ROOT, "bot.py")], { env, stdio: "ignore" });
}

async function waitReady() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(BASE + "/health");
      if (r.status === 200) return;
    } catch (e) { /* ещё не поднялся */ }
    await sleep(300);
  }
  throw new Error("сервер не поднялся");
}

// Опрос сервера до выполнения условия; при таймауте кидает ошибку
// со снимком последнего состояния.
async function waitServer(cond, tries) {
  const limit = tries || 40;
  let last = null;
  for (let i = 0; i < limit; i++) {
    try {
      const r = await fetch(BASE + "/api/full?user_id=999999");
      const data = await r.json();
      last = data;
      if (cond(data)) return data;
    } catch (e) { /* ждём дальше */ }
    await sleep(250);
  }
  const err = new Error("server condition not met in time");
  err.snapshot = last;
  throw err;
}

// Опрос DOM до выполнения условия.
async function waitDom(cond, tries) {
  const limit = tries || 40;
  for (let i = 0; i < limit; i++) {
    try {
      if (cond()) return true;
    } catch (e) { /* ждём дальше */ }
    await sleep(60);
  }
  return false;
}


async function main() {
  const port = await freePort();
  BASE = `http://127.0.0.1:${port}`;
  const server = startServer(port);
  try {
    await waitReady();

    const html = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
    const appJs = fs.readFileSync(path.join(ROOT, "web", "app.js"), "utf8");

    const dom = new JSDOM(html, {
      url: BASE + "/",
      runScripts: "outside-only",
      pretendToBeVisual: true, // даёт requestAnimationFrame
    });
    const { window } = dom;
    window.Telegram = undefined; // localhost -> демо-режим
    window.fetch = (url, opts) => fetch(new URL(url, BASE).href, opts);
    window.AbortController = AbortController;
    window.scrollTo = () => {}; // jsdom его не реализует

    dom.window.eval(appJs);
    await sleep(900);

    const doc = window.document;
    const $ = (id) => doc.getElementById(id);
    const q = (sel) => doc.querySelector(sel);
    const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const submit = (el) => el.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));

    check("бут прошёл", window.__booted === true);
    check("заголовок по умолчанию", $("mastheadTitle").textContent === "slaughter_lord");
    check("храм виден", !$("view-temple").hidden);
    check("остальные скрыты", $("view-tasks").hidden && $("view-habits").hidden);
    check("бейдж ДЕМО", $("mastheadBadge").textContent === "ДЕМО");

    // --- умное добавление задачи (оптимистично) ---
    $("templeQuickInput").value = "завтра 09:00 купить кровь #ритуал !важно";
    submit($("templeQuickForm"));
    // задача «на завтра» не показывается в храме — идём в список
    click(q('[data-view="tasks"]'));
    await sleep(150);
    check("задача появилась мгновенно", q("#taskList .task") !== null);
    check("тег распарсился", q("#taskList .tag-pill") !== null);

    let remote = await waitServer((data) => data.tasks.length > 0);
    check("задача дошла до сервера", remote.tasks.some((t) => t.title.includes("кровь")));
    check("тег на сервере", remote.tags.some((t) => t.name === "ритуал"));
    check("дата завтра", remote.tasks[0].due !== null);
    check("важность", remote.tasks[0].important === 1);
    const serverTaskId = remote.tasks[0].id;

    // --- переключение задачи ---
    click(q("#taskList .task-toggle"));
    await sleep(150);
    const taskEl = q("#taskList .task");
    check("toggle применился локально", !taskEl || taskEl.classList.contains("done"));
    await waitServer((data) => (data.tasks.find((t) => t.id === serverTaskId) || {}).done === 1);
    check("toggle дошёл до сервера", true);

    // --- фильтры, календарь, канбан ---
    click(q('[data-tview="cal"]'));
    await sleep(120);
    check("календарь показан", !$("calWrap").hidden);
    check("сетка календаря построена", doc.querySelectorAll("#calGrid .cal-day").length > 30);
    check("метка дня есть", doc.querySelectorAll("#calGrid .cal-dot").length >= 1);

    click(q('[data-tview="kanban"]'));
    await sleep(120);
    check("канбан показан", !$("kanbanWrap").hidden);
    check("3 колонки", doc.querySelectorAll("#kanbanCols .kan-col").length === 3);
    click(q('[data-tview="list"]'));
    await sleep(120);

    // --- модалка ---
    click(q("#taskList .task-body"));
    await sleep(120);
    check("модалка открылась", !$("modal").hidden);
    $("mTitle").value = "Переименовано";
    $("mTitle").dispatchEvent(new window.Event("change", { bubbles: true }));
    await waitServer((data) => (data.tasks.find((t) => t.id === serverTaskId) || {}).title === "Переименовано");
    check("переименование дошло", true);
    click(q('[data-action="modal-close"]'));
    await sleep(100);
    check("модалка закрылась", $("modal").hidden);

    // --- привычки ---
    click(q('[data-view="habits"]'));
    $("habitInput").value = "Утренняя гимнастика";
    submit($("habitForm"));
    check("привычка появилась мгновенно", await waitDom(() => q(".habit-card") !== null));
    check("сетка 28 дней", doc.querySelectorAll(".habit-day").length === 28);
    click(q(".habit-day button"));
    await waitServer((data) => data.habits.length > 0 && data.habits[0].days.length >= 1);
    check("отметка дня сохранилась", true);

    // --- дневник ---
    click(q('[data-view="diary"]'));
    $("diaryInput").value = "Сегодня был хороший день";
    click($("diarySave"));
    await waitServer((data) => data.diary.length === 1);
    check("запись дневника сохранилась", true);
    check("запись видна в списке", q("#diaryList .diary-entry") !== null);

    // --- статистика и помодоро ---
    click(q('[data-view="stats"]'));
    await sleep(150);
    check("карточки статистики", doc.querySelectorAll("#statsGrid .stat-card").length === 4);
    check("помодоро 25:00", $("pomoDisplay").textContent === "25:00");
    click($("pomoToggle"));
    await sleep(150);
    check("таймер запущен", $("pomoToggle").textContent === "Стоп");
    click($("pomoToggle"));
    await sleep(150);
    check("таймер остановлен", $("pomoToggle").textContent === "Старт");

    // --- настройки и проекты ---
    click(q('[data-view="cult"]'));
    await sleep(150);
    $("projectInput").value = "Кровавый культ";
    $("projectIcon").value = "🩸";
    submit($("projectForm"));
    await waitServer((data) => data.projects.some((p) => p.name === "Кровавый культ"));
    check("проект создан на сервере", true);

    $("setTitle").value = "night_lord";
    $("setTitle").dispatchEvent(new window.Event("input", { bubbles: true }));
    await waitServer((data) => data.profile.title === "night_lord");
    check("титул сохранился", true);
    check("титул в шапке", $("mastheadTitle").textContent === "night_lord");

    click(q('[data-mode="void"]'));
    await sleep(150);
    check("режим void применился", doc.body.dataset.mode === "void");

    // --- офлайн-поведение ---
    server.kill("SIGTERM");
    await sleep(600);
    $("projectInput").value = "Офлайн-проект";
    submit($("projectForm"));
    check("бейдж «нет связи»", await waitDom(() => !$("netBadge").hidden));
    const queueRaw = window.localStorage.getItem("tj.queue.v2");
    check("операция легла в очередь", queueRaw && queueRaw.includes("project.add"));

    console.log(results.every(Boolean) ? "E2E PASS" : "E2E FAIL");
  } finally {
    try { server.kill("SIGKILL"); } catch (e) {}
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

