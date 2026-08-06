// Тестовая консоль: запуск прогона + поллинг статуса (зеркало prompt_regression.js).
// POST start -> {ok, run_id, run}; GET status -> {ok, run}; поллинг setTimeout 1500ms,
// пока run.status === "running". Все динамические тексты (method/class_ru/traceback/log)
// вставляются через textContent/escapeHtml — без raw innerHTML.
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
  var resultsCard = document.getElementById("tcResultsCard");
  var resultsList = document.getElementById("tcResultsList");
  var logBox = document.getElementById("tcLog");

  var currentRunId = "";
  var pollTimer = null;

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

  function statusBadge(status, status_ru) {
    var cls = "tc-badge " + escapeHtml(status || "");
    return '<span class="' + cls + '">' + escapeHtml(status_ru || status || "") + '</span>';
  }
  function renderSummary(s) {
    if (!s) { if (summaryCard) summaryCard.style.display = "none"; if (summaryBox) summaryBox.textContent = ""; return; }
    var lines = [
      "Всего: " + Number(s.ran || 0),
      "Результат: " + (s.ok ? "ОК" : "есть провалы/ошибки"),
      "Провалов: " + Number(s.failures || 0),
      "Ошибок: " + Number(s.errors || 0),
      "Пропущено: " + Number(s.skipped || 0),
      "Ожидаемых провалов: " + Number(s.expected_failures || 0),
      "Неожиданных успехов: " + Number(s.unexpected_successes || 0),
      "Время: " + Number(s.seconds || 0) + " с"
    ];
    if (summaryBox) {
      summaryBox.innerHTML = lines.map(function (l) {
        return '<div class="tc-summary-line">' + escapeHtml(l) + '</div>';
      }).join("");
    }
    if (summaryCard) summaryCard.style.display = "block";
  }
  function renderResults(results) {
    if (!resultsList) return;
    if (!Array.isArray(results) || !results.length) {
      resultsList.textContent = "";
      if (resultsCard) resultsCard.style.display = "none";
      return;
    }
    resultsList.textContent = "";
    results.forEach(function (r) {
      var row = document.createElement("div");
      row.className = "tc-result-row";
      row.innerHTML =
        statusBadge(r.status, r.status_ru) +
        '<span class="tc-method"></span>' +
        '<span class="tc-class-ru"></span>';
      var methodSpan = row.querySelector(".tc-method");
      var classSpan = row.querySelector(".tc-class-ru");
      if (methodSpan) methodSpan.textContent = r.method || "";
      if (classSpan) classSpan.textContent = r.class_ru || r.class || "";
      resultsList.appendChild(row);
      if (r.traceback) {
        var pre = document.createElement("pre");
        pre.className = "tc-tb";
        pre.textContent = r.traceback;
        resultsList.appendChild(pre);
      }
    });
    if (resultsCard) resultsCard.style.display = "block";
  }
  function renderLog(log) {
    if (logBox) logBox.textContent = Array.isArray(log) ? log.join("\n") : "";
  }

  function applyRunSnapshot(run) {
    if (!run) return;
    renderResults(run.results || []);
    renderSummary(run.summary || null);
    renderLog(run.log || []);
    if (run.status === "running") {
      var c = run.current ? " | Сейчас: " + run.current : "";
      setRunProgress("Прогон: " + Number(run.completed || 0) + (run.total ? "/" + Number(run.total) : "") + c);
      setRunError("");
      setSubmitDisabled(true);
      return;
    }
    if (run.status === "completed") {
      setRunProgress("Прогон завершён: " + Number(run.completed || 0) + "/" + Number(run.total || 0));
      setRunError("");
      setSubmitDisabled(false);
      return;
    }
    if (run.status === "failed") {
      setRunProgress("");
      setRunError(run.error_message || "Прогон завершился с ошибкой");
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
    setRunProgress("Запускаем тесты…");
    renderSummary(null);
    renderResults([]);
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