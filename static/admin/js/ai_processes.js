// Личное меню «мои процессы» в шапке админки (base_site.html, welcome-msg).
// Поллинг /ai/admin/active-runs/ каждые 15 c (+ на visibilitychange и сразу при
// открытии меню): список СВОИХ прогонов (arm solve/find-error, регрессия
// промптов, тестовая консоль) + глобальная запись обновления моделей —
// тип, этап, прогресс N/M с процентом; завершённые — 10 минут с бейджем итога.
// Страницы прогонов после успешного старта диспатчат событие
// «ai-processes:refresh» — процесс появляется в меню сразу.
// Показаны 3 записи, дальше — плавный скролл. Прогон, завершившийся вдали
// от своей страницы, получает одноразовый тост со ссылкой на страницу
// прогона (?run_id=), где результат восстанавливается.
//
// Рендер только createElement + textContent — данные модели/этапы приходят
// от пользователей и моделей, никакого innerHTML с ними (CLAUDE.md: escaping).
(function () {
  "use strict";

  var ACTIVE_RUNS_URL = "/ai/admin/active-runs/";
  var POLL_INTERVAL_MS = 15000;
  var TOAST_TTL_MS = 10000;
  var NOTIFIED_KEY_PREFIX = "ai_proc_notified_";

  var TYPE_LABELS = {
    batch: "ARM: пакетное решение",
    single: "ARM: поиск ошибки",
    prompt_regression: "Регрессия промптов",
    test_console: "Тестовая консоль",
    // Sweep общий (ручной запуск / планировщик 04:00) — запись глобальная,
    // без владельца: видна каждому админу.
    model_refresh: "Обновление моделей"
  };
  var STATUS_LABELS = {
    completed: "Завершён",
    failed: "Ошибка",
    cancelled: "Прерван"
  };

  var toggle = document.getElementById("aiProcessesToggle");
  var chip = document.getElementById("aiProcessesChip");
  var menu = document.getElementById("aiProcessesMenu");
  if (!toggle || !chip || !menu) return;

  var polling = false;
  var lastRuns = [];

  function runUrl(run) {
    var url = String(run.page_url || "");
    if (run.run_id) {
      url += (url.indexOf("?") >= 0 ? "&" : "?") + "run_id=" + encodeURIComponent(run.run_id);
    }
    return url;
  }

  function openMenu() {
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    // Открыли меню — сразу свежие данные, без ожидания планового тика.
    poll();
  }

  function closeMenu() {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  }

  toggle.addEventListener("click", function () {
    if (menu.hidden) { openMenu(); } else { closeMenu(); }
  });

  document.addEventListener("mousedown", function (event) {
    if (!menu.hidden && !toggle.contains(event.target) && !menu.contains(event.target)) {
      closeMenu();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !menu.hidden) {
      closeMenu();
      toggle.focus();
    }
  });

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null && text !== "") node.textContent = String(text);
    return node;
  }

  function renderChip(runs) {
    var activeCount = 0;
    for (var i = 0; i < runs.length; i++) {
      if (runs[i].status === "running") activeCount += 1;
    }
    chip.textContent = "Процессы (" + activeCount + ") ▾";
    chip.classList.toggle("idle", activeCount === 0 && runs.length === 0);
  }

  function buildItem(run) {
    var item = el("div", "ai-processes-item");
    var title = el("div", "ai-processes-item-title");
    title.appendChild(el("span", null, TYPE_LABELS[run.run_type] || run.run_type));
    if (run.status && run.status !== "running" && STATUS_LABELS[run.status]) {
      title.appendChild(el("span", "ai-processes-status " + run.status, STATUS_LABELS[run.status]));
    }
    item.appendChild(title);

    if (run.current) {
      item.appendChild(el("div", "ai-processes-item-stage", run.current));
    }

    var total = Number(run.total || 0);
    var completed = Number(run.completed || 0);
    var progress = el("div", "ai-processes-progress");
    if (total > 0) {
      // Проценты считаем на фронте: в бекенде только счётчики N/M.
      var percent = run.status === "running"
        ? Math.min(100, Math.round((completed / total) * 100))
        : 100;
      progress.appendChild(el("div", "ai-processes-progress-fill"));
      progress.firstChild.style.width = percent + "%";
      item.appendChild(el("div", "ai-processes-item-stage",
        completed + " / " + total + " · " + percent + "%"));
    } else if (run.status === "running") {
      // У тестовой консоли total неизвестен до итоговой строки unittest.
      progress.classList.add("indeterminate");
      progress.appendChild(el("div", "ai-processes-progress-fill"));
      item.appendChild(el("div", "ai-processes-item-stage", completed + " выполнено"));
    }
    item.appendChild(progress);

    var link = el("a", "ai-processes-link", "Открыть →");
    link.href = runUrl(run);
    item.appendChild(link);
    return item;
  }

  function renderMenu(runs) {
    menu.textContent = "";
    // Высота авто, пока пунктов не больше трёх.
    menu.style.maxHeight = "";
    if (!runs.length) {
      menu.appendChild(el("div", "ai-processes-empty", "Нет активных прогонов"));
      return;
    }
    var items = [];
    for (var i = 0; i < runs.length; i++) {
      var item = buildItem(runs[i]);
      items.push(item);
      menu.appendChild(item);
    }
    // Показываем 3 записи, остальное — плавный скролл (CSS scroll-behavior).
    if (items.length > 3) {
      var third = items[2];
      menu.style.maxHeight = (third.offsetTop + third.offsetHeight) + "px";
    }
  }

  function showToast(run) {
    var toast = el("div", "ai-processes-toast");
    if (run.status && STATUS_LABELS[run.status] && run.status !== "completed") {
      toast.classList.add(run.status);
    }
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.appendChild(el("div", "ai-processes-toast-title",
      run.status === "failed" ? "Прогон завершился с ошибкой" : "Прогон завершён"));
    toast.appendChild(el("div", "ai-processes-toast-meta",
      (TYPE_LABELS[run.run_type] || run.run_type) +
      (STATUS_LABELS[run.status] ? " — " + STATUS_LABELS[run.status] : "")));
    var link = el("a", null, "Открыть страницу прогона →");
    link.href = runUrl(run);
    toast.appendChild(link);
    var close = el("button", "ai-processes-toast-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть");
    toast.appendChild(close);

    var removed = false;
    function remove() {
      if (removed) return;
      removed = true;
      window.clearTimeout(ttlTimer);
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }
    close.addEventListener("click", remove);
    link.addEventListener("click", remove);
    var ttlTimer = window.setTimeout(remove, TOAST_TTL_MS);
    document.body.appendChild(toast);
  }

  function notifyFinished(runs) {
    for (var i = 0; i < runs.length; i++) {
      var run = runs[i];
      if (run.status === "running" || !run.run_id) continue;
      var key = NOTIFIED_KEY_PREFIX + run.run_id;
      try {
        if (window.sessionStorage.getItem(key)) continue;
        window.sessionStorage.setItem(key, "1");
      } catch (e) { continue; } // sessionStorage недоступен — без дедупликации
      // На самой странице прогона итог уже показан — тост не нужен.
      if (String(run.page_url || "") === window.location.pathname) continue;
      showToast(run);
    }
  }

  function poll() {
    if (polling || document.hidden) return;
    polling = true;
    fetch(ACTIVE_RUNS_URL, { credentials: "same-origin" })
      .then(function (resp) { polling = false; return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data || !data.ok) return;
        lastRuns = data.runs || [];
        renderChip(lastRuns);
        renderMenu(lastRuns);
        notifyFinished(lastRuns);
      })
      .catch(function () { polling = false; });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { poll(); }
  });
  // Страницы прогонов (arm solve/find-error, регрессия, тестовая консоль,
  // обновление моделей) диспатчат это событие после успешного старта —
  // процесс попадает в меню сразу, не дожидаясь планового тика.
  var refreshDebounceTimer = null;
  document.addEventListener("ai-processes:refresh", function () {
    if (refreshDebounceTimer) { window.clearTimeout(refreshDebounceTimer); }
    refreshDebounceTimer = window.setTimeout(poll, 300);
  });
  poll();
  window.setInterval(poll, POLL_INTERVAL_MS);
})();