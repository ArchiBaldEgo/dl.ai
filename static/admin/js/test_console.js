// Тестовая консоль: запуск прогона + поллинг статуса (зеркало prompt_regression.js).
// POST start -> {ok, run_id, run}; GET status -> {ok, run}; поллинг setTimeout 1500ms,
// пока run.status === "running".
//
// Журнал вывода — массив записей {"text","kind"}: понятный человеческий текст
// (stage/ok/fail/skip/warn/human/final-ok/final-fail) рисуется крупно и с цветом,
// технический шум (raw — трейсбеки, вывод логгеров) — приглушённым моноширинным
// шрифтом. Каждая запись рендерится отдельной строкой через textContent (без raw innerHTML).
(function () {
  "use strict";
  var config = window.TEST_CONSOLE_CONFIG || {};
  var START_URL = config.startUrl || "/ai/admin/test-console/start/";
  var STATUS_URL = config.statusUrl || "/ai/admin/test-console/status/";

  var runForm = document.getElementById("tcRunForm");
  if (!runForm) return;
  var csrfInput = runForm.querySelector('input[name="csrfmiddlewaretoken"]');
  var submitButton = document.getElementById("tcRunSubmitBtn");
  var runProgress = document.getElementById("tcRunProgress");
  var runError = document.getElementById("tcRunError");
  var summaryCard = document.getElementById("tcSummaryCard");
  var summaryBox = document.getElementById("tcSummary");
  var simpleCard = document.getElementById("tcSimpleCard");
  var simpleBanner = document.getElementById("tcSimpleBanner");
  var simpleResults = document.getElementById("tcSimpleResults");
  var logBox = document.getElementById("tcLog");

  var currentRunId = "";
  var pollTimer = null;

  // Допустимые виды подсветки журнала (маппятся 1:1 на CSS-классы .tc-log-<kind>).
  var KNOWN_KINDS = {
    raw: 1, stage: 1, ok: 1, fail: 1, skip: 1, warn: 1,
    human: 1, "final-ok": 1, "final-fail": 1
  };

  function escapeHtml(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }
  function getCookie(n) {
    var parts = (document.cookie || "").split("; ");
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].indexOf(n + "=") === 0) {
        return decodeURIComponent(parts[i].slice(n.length + 1));
      }
    }
    return "";
  }
  function getCsrfToken() {
    return getCookie("csrftoken") || (csrfInput ? csrfInput.value : "");
  }
  function setSubmitDisabled(d) { if (submitButton) submitButton.disabled = !!d; }
  function setRunError(m) {
    if (!runError) return;
    runError.textContent = m || "";
    runError.style.display = m ? "block" : "none";
  }
  function setRunProgress(t) {
    if (!runProgress) return;
    var pt = runProgress.querySelector(".tc-progress-text");
    if (pt) pt.textContent = t || "";
    if (t) runProgress.classList.add("active"); else runProgress.classList.remove("active");
  }

  function renderSummary(s) {
    // Упрощённая сводка для не-технического пользователя: показываем только
    // осмысленные строки, без жаргона «ожидаемых провалов/неожиданных успехов».
    if (!s) { if (summaryCard) summaryCard.style.display = "none"; if (summaryBox) summaryBox.textContent = ""; return; }
    var ran = Number(s.ran || 0);
    var failures = Number(s.failures || 0);
    var errors = Number(s.errors || 0);
    var skipped = Number(s.skipped || 0);
    var lines = [
      "Проверено сценариев: " + ran,
      "Результат: " + (s.ok ? "все прошли" : "есть провалы/ошибки"),
    ];
    if (failures > 0) lines.push("Провалов: " + failures);
    if (errors > 0) lines.push("Ошибок: " + errors);
    if (skipped > 0) lines.push("Пропущено: " + skipped);
    lines.push("Время: " + Number(s.seconds || 0) + " с");
    if (summaryBox) {
      summaryBox.innerHTML = lines.map(function (l) {
        return '<div class="tc-summary-line">' + escapeHtml(l) + '</div>';
      }).join("");
    }
    if (summaryCard) summaryCard.style.display = "block";
  }

  function renderSimpleBanner(s) {
    // Крупный однострочный итог поверх всего — то, что видит не знающий пользователь.
    if (!s || !simpleBanner) { if (simpleBanner) simpleBanner.style.display = "none"; return; }
    simpleBanner.className = "tc-banner " + (s.ok ? "ok" : "fail");
    if (s.ok) {
      simpleBanner.innerHTML = escapeHtml("✅ Все проверки прошли успешно!") +
        '<small>' + escapeHtml("Проверено " + Number(s.ran || 0) + " сценариев за " + Number(s.seconds || 0) + " с.") + '</small>';
    } else {
      var bad = Number(s.failures || 0) + Number(s.errors || 0);
      simpleBanner.innerHTML = escapeHtml("❌ Есть проблемы" + (bad ? " (" + bad + ")" : "") + ".") +
        '<small>' + escapeHtml("Подробности — в журнале ниже.") + '</small>';
    }
    simpleBanner.style.display = "block";
  }

  function renderSimpleResults(results) {
    // Группировка по классу (русское название), без имён методов и трейсбеков.
    // Маркер группы: ✓ если все прошли, ✗ если хоть один провал/ошибка, ◐ иначе.
    if (!simpleResults || !simpleCard) return;
    simpleResults.textContent = "";
    if (!Array.isArray(results) || !results.length) {
      simpleCard.style.display = "none";
      return;
    }
    var groups = {}; // class_ru -> {ok, bad, skipped, order}
    var order = [];
    results.forEach(function (r) {
      var name = (r && r.class_ru) ? String(r.class_ru) : (r && r.class ? String(r.class) : "—");
      var status = r ? r.status : "ok";
      if (!groups[name]) { groups[name] = { ok: 0, bad: 0, skipped: 0 }; order.push(name); }
      if (status === "ok") groups[name].ok += 1;
      else if (status === "skipped" || status === "expected_failure") groups[name].skipped += 1;
      else groups[name].bad += 1;
    });
    var html = order.map(function (name) {
      var g = groups[name];
      var mark, cls;
      if (g.bad > 0) { mark = "✗"; cls = "fail"; }
      else if (g.ok > 0 && g.skipped === 0) { mark = "✓"; cls = "ok"; }
      else { mark = "◐"; cls = ""; }
      var countParts = [];
      if (g.ok) countParts.push(g.ok + " прошло");
      if (g.bad) countParts.push(g.bad + " не прошло");
      if (g.skipped) countParts.push(g.skipped + " пропущено");
      var count = countParts.join(" · ");
      return '<div class="tc-group-row">' +
        '<span class="tc-gmark ' + cls + '">' + escapeHtml(mark) + '</span>' +
        '<span class="tc-gname">' + escapeHtml(name) + '</span>' +
        '<span class="tc-gcount">' + escapeHtml(count) + '</span>' +
        '</div>';
    }).join("");
    simpleResults.innerHTML = html;
    simpleCard.style.display = "block";
  }

  function renderLog(log) {
    if (!logBox) return;
    logBox.textContent = "";
    if (!Array.isArray(log) || !log.length) return;
    log.forEach(function (entry) {
      var text, kind;
      if (entry && typeof entry === "object") {
        text = entry.text == null ? "" : String(entry.text);
        kind = entry.kind || "raw";
      } else {
        text = entry == null ? "" : String(entry);
        kind = "raw";
      }
      if (!Object.prototype.hasOwnProperty.call(KNOWN_KINDS, kind)) kind = "raw";
      var line = document.createElement("div");
      line.className = "tc-log-line " + kind;
      line.textContent = text;
      logBox.appendChild(line);
    });
    // Держать журнал прокрученным к свежим строкам.
    logBox.scrollTop = logBox.scrollHeight;
  }

  function applyRunSnapshot(run) {
    if (!run) return;
    renderSummary(run.summary || null);
    renderSimpleBanner(run.summary || null);
    renderSimpleResults(run.results || []);
    renderLog(run.log || []);
    if (run.status === "running") {
      var c = run.current ? " | Сейчас: " + run.current : "";
      setRunProgress("Проверено " + Number(run.completed || 0) + (run.total ? " из " + Number(run.total) : "") + c);
      setRunError("");
      setSubmitDisabled(true);
      return;
    }
    if (run.status === "completed") {
      setRunProgress("Проверки завершены: " + Number(run.completed || 0) + " из " + Number(run.total || 0));
      setRunError("");
      setSubmitDisabled(false);
      return;
    }
    if (run.status === "failed") {
      setRunProgress("");
      setRunError(run.error_message || "Проверки завершились с ошибкой");
      setSubmitDisabled(false);
    }
  }

  function pollRunStatus() {
    if (!currentRunId) return;
    fetch(STATUS_URL + "?run_id=" + encodeURIComponent(currentRunId), {
      method: "GET", credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (r) { return r.json().then(function (d) { return { response: r, data: d }; }); })
      .then(function (res) {
        if (!res.response.ok || !res.data.ok) throw new Error(res.data.message || "Не удалось получить статус");
        var run = res.data.run || {};
        applyRunSnapshot(run);
        if (run.status === "running") pollTimer = window.setTimeout(pollRunStatus, 1500);
      })
      .catch(function (e) {
        setRunError(e.message || "Ошибка обновления статуса");
        setSubmitDisabled(false);
      });
  }

  runForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submitButton && submitButton.disabled) return;
    if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null; }
    setSubmitDisabled(true);
    setRunError("");
    setRunProgress("Запускаем проверки…");
    renderSummary(null);
    renderSimpleBanner(null);
    renderSimpleResults([]);
    renderLog([]);
    fetch(START_URL, {
      method: "POST", credentials: "same-origin",
      headers: {
        "X-CSRFToken": getCsrfToken(),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
      },
      body: new URLSearchParams(new FormData(runForm)).toString()
    })
      .then(function (r) { return r.json().then(function (d) { return { response: r, data: d }; }); })
      .then(function (res) {
        if (!res.response.ok || !res.data.ok) throw new Error(res.data.message || "Не удалось запустить");
        currentRunId = res.data.run_id || "";
        if (currentRunId && window.history && window.history.replaceState) {
          window.history.replaceState({}, "", window.location.pathname + "?run_id=" + encodeURIComponent(currentRunId));
        }
        applyRunSnapshot(res.data.run || {});
        if (res.data.run && res.data.run.status === "running") pollRunStatus();
      })
      .catch(function (e) {
        setRunError(e.message || "Ошибка запуска");
        setRunProgress("");
        setSubmitDisabled(false);
      });
  });

  var initialNode = document.getElementById("test-console-initial-run");
  var initialRun = {};
  if (initialNode) {
    try { initialRun = JSON.parse(initialNode.textContent || "{}") || {}; } catch (e) { initialRun = {}; }
  }
  if (initialRun && initialRun.run_id) {
    currentRunId = initialRun.run_id;
    applyRunSnapshot(initialRun);
    if (initialRun.status === "running") pollRunStatus();
  }
})();