/* Prompt regression tests admin page.
 *
 * Mirrors the inlined ARM admin JS (ai/templates/admin/ai/arm_find_error.html):
 * CSRF retrieval, start-run POST, 1.5s status polling, report + result render.
 * URLs and the initial run snapshot are injected from the template via
 * window.PROMPT_REGRESSION_CONFIG and the #prompt-regression-initial-run
 * json_script tag (the JS file cannot use Django template tags).
 */
(function () {
  "use strict";

  var config = window.PROMPT_REGRESSION_CONFIG || {};
  var START_URL = config.startUrl || "/ai/admin/prompt-regression/start/";
  var STATUS_URL = config.statusUrl || "/ai/admin/prompt-regression/status/";

  var runForm = document.getElementById("prtRunForm");
  if (!runForm) {
    return;
  }

  var csrfInput = runForm.querySelector('input[name="csrfmiddlewaretoken"]');
  var submitButton = document.getElementById("prtRunSubmitBtn");
  var runProgress = document.getElementById("prtRunProgress");
  var runError = document.getElementById("prtRunError");
  var reportCard = document.getElementById("prtReportCard");
  var reportList = document.getElementById("prtReportList");
  var mismatchesWrap = document.getElementById("prtMismatchesWrap");
  var resultsCard = document.getElementById("prtResultsCard");
  var resultsList = document.getElementById("prtResultsList");

  var currentRunId = "";
  var pollTimer = null;

  var initialRun = {};
  var initialNode = document.getElementById("prompt-regression-initial-run");
  if (initialNode) {
    try {
      initialRun = JSON.parse(initialNode.textContent || "{}") || {};
    } catch (e) {
      initialRun = {};
    }
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function getCookie(name) {
    var cookieValue = document.cookie
      .split("; ")
      .find(function (row) { return row.startsWith(name + "="); });
    return cookieValue ? decodeURIComponent(cookieValue.split("=").slice(1).join("=")) : "";
  }

  function getCsrfToken() {
    return getCookie("csrftoken") || (csrfInput ? csrfInput.value : "");
  }

  function setSubmitDisabled(disabled) {
    if (submitButton) {
      submitButton.disabled = !!disabled;
    }
  }

  function setRunError(message) {
    if (!runError) return;
    runError.textContent = message || "";
    runError.style.display = message ? "block" : "none";
  }

  function setRunProgress(text) {
    if (!runProgress) return;
    var progressText = runProgress.querySelector(".prt-progress-text");
    if (progressText) {
      progressText.textContent = text || "";
    }
    if (text) {
      runProgress.classList.add("active");
    } else {
      runProgress.classList.remove("active");
    }
  }

  function verdictBadge(verdict) {
    if (verdict === "match") return '<span class="prt-badge match">Совпадает</span>';
    if (verdict === "skipped") return '<span class="prt-badge skipped">Пропущен</span>';
    return '<span class="prt-badge mismatch">Отклонение</span>';
  }

  function renderReport(report) {
    if (!report) {
      if (reportList) reportList.innerHTML = "";
      if (mismatchesWrap) mismatchesWrap.innerHTML = "";
      if (reportCard) reportCard.style.display = "none";
      return;
    }
    if (reportList) {
      reportList.innerHTML = [
        "Всего кейсов: " + Number(report.total || 0),
        "Совпадает: " + Number(report.matched || 0),
        "Отклонений: " + Number(report.mismatched || 0),
        "Пропущено: " + Number(report.skipped || 0),
        "Суммарно токенов: " + Number(report.tokens_total || 0),
      ].map(function (line) { return "<li>" + escapeHtml(line) + "</li>"; }).join("");
    }
    renderMismatches(report.mismatches || []);
    if (reportCard) reportCard.style.display = "block";
  }

  function renderMismatches(mismatches) {
    if (!mismatchesWrap) return;
    if (!Array.isArray(mismatches) || mismatches.length === 0) {
      mismatchesWrap.innerHTML = '<p class="prt-empty">Отклонений от эталона нет.</p>';
      return;
    }
    var html = '<h3>Отклонения от эталона</h3>';
    mismatches.forEach(function (m) {
      html += '<details class="prt-result mismatch">'
        + "<summary>"
        + '<div class="prt-result-top">'
        + '<span class="prt-result-title">' + escapeHtml(m.case_name || "—") + "</span>"
        + verdictBadge(m.verdict)
        + '<span class="prt-result-hint">' + escapeHtml(m.diff_hint || "") + "</span>"
        + "</div>"
        + "</summary>"
        + '<div class="prt-result-body">'
        + "<div><h4>Эталон</h4><pre></pre></div>"
        + "<div><h4>Реакция модели</h4><pre></pre></div>"
        + "</div>"
        + "</details>";
    });
    mismatchesWrap.innerHTML = html;
    var blocks = mismatchesWrap.querySelectorAll(".prt-result");
    blocks.forEach(function (block, index) {
      var m = mismatches[index];
      var pres = block.querySelectorAll("pre");
      if (pres[0]) pres[0].textContent = m.expected || "—";
      if (pres[1]) pres[1].textContent = m.actual || "—";
    });
  }

  function renderResults(results) {
    if (!resultsList) return;
    if (!Array.isArray(results) || results.length === 0) {
      resultsList.innerHTML = "";
      if (resultsCard) resultsCard.style.display = "none";
      return;
    }
    resultsList.innerHTML = "";
    results.forEach(function (item) {
      var card = document.createElement("details");
      card.className = "prt-result " + (item.verdict || "mismatch");
      card.innerHTML = [
        "<summary>",
        '<div class="prt-result-top">',
        '<span class="prt-result-title">' + escapeHtml(item.case_name || "—") + "</span>",
        verdictBadge(item.verdict),
        '<span class="prt-result-hint">'
          + escapeHtml(item.mode || "") + " · " + escapeHtml(String(item.duration || 0)) + " сек · "
          + escapeHtml(String(item.tokens || 0)) + " ток."
          + (item.diff_hint ? " · " + escapeHtml(item.diff_hint) : "")
          + "</span>",
        "</div>",
        "</summary>",
        '<div class="prt-result-body">'
          + "<div><h4>Эталон</h4><pre></pre></div>"
          + "<div><h4>Реакция модели</h4><pre></pre></div>"
          + "</div>",
      ].join("");
      var pres = card.querySelectorAll("pre");
      if (pres[0]) pres[0].textContent = item.expected || "—";
      if (pres[1]) pres[1].textContent = item.actual || "—";
      resultsList.appendChild(card);
    });
    if (resultsCard) resultsCard.style.display = "block";
  }

  function applyRunSnapshot(run) {
    if (!run) return;
    var completed = Number(run.completed_cases || 0);
    var total = Number(run.total_cases || 0);
    var currentCase = run.current_case_name || "";

    renderResults(run.results || []);
    renderReport(run.report || null);

    if (run.status === "running") {
      var suffix = currentCase ? " | Сейчас: " + currentCase : "";
      runProgress.classList.add("active");
      setRunProgress("Прогон: " + completed + "/" + total + suffix);
      setRunError("");
      setSubmitDisabled(true);
      return;
    }
    if (run.status === "completed") {
      runProgress.classList.remove("active");
      setRunProgress("Прогон завершён: " + completed + "/" + total);
      setRunError("");
      setSubmitDisabled(false);
      return;
    }
    if (run.status === "failed") {
      runProgress.classList.remove("active");
      setRunProgress("");
      setRunError(run.error_message || "Прогон завершился с ошибкой");
      setSubmitDisabled(false);
    }
  }

  function pollRunStatus() {
    if (!currentRunId) return;
    fetch(STATUS_URL + "?run_id=" + encodeURIComponent(currentRunId), {
      method: "GET",
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (response) { return response.json().then(function (data) { return { response: response, data: data }; }); })
      .then(function (res) {
        if (!res.response.ok || !res.data.ok) {
          throw new Error(res.data.message || "Не удалось получить статус прогона");
        }
        var run = res.data.run || {};
        applyRunSnapshot(run);
        if (run.status === "running") {
          pollTimer = window.setTimeout(pollRunStatus, 1500);
        }
      })
      .catch(function (error) {
        setRunError(error.message || "Ошибка обновления статуса прогона");
        setSubmitDisabled(false);
      });
  }

  runForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submitButton && submitButton.disabled) return;
    if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null; }

    setSubmitDisabled(true);
    setRunError("");
    runProgress.classList.add("active");
    setRunProgress("Запускаем прогон...");
    renderReport(null);
    renderResults([]);

    var formData = new FormData(runForm);
    var body = new URLSearchParams(formData);

    fetch(START_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": getCsrfToken(),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: body.toString(),
    })
      .then(function (response) { return response.json().then(function (data) { return { response: response, data: data }; }); })
      .then(function (res) {
        if (!res.response.ok || !res.data.ok) {
          throw new Error(res.data.message || "Не удалось запустить прогон");
        }
        currentRunId = res.data.run_id || "";
        if (currentRunId && window.history && window.history.replaceState) {
          var newUrl = window.location.pathname + "?run_id=" + encodeURIComponent(currentRunId);
          window.history.replaceState({}, "", newUrl);
        }
        applyRunSnapshot(res.data.run || {});
        if (res.data.run && res.data.run.status === "running") {
          pollRunStatus();
        }
      })
      .catch(function (error) {
        setRunError(error.message || "Ошибка запуска прогона");
        setRunProgress("");
        setSubmitDisabled(false);
      });
  });

  if (initialRun && initialRun.run_id) {
    currentRunId = initialRun.run_id;
    applyRunSnapshot(initialRun);
    if (initialRun.status === "running") {
      pollRunStatus();
    }
  }
})();