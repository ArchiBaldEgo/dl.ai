// Сохранение состояния страниц админки (base_site.html, все страницы AI admin).
//
// 1) Changelist'ы (Django #changelist): querystring с фильтрами/поиском/
//    сортировкой/пагинацией сохраняется per-pathname; при возврате на страницу
//    «со стороны» (referrer того же origin с другим pathname — со страницы
//    объекта, по ссылке сайдбара) — восстанавливается через location.replace.
//    Возврат с этой же страницы без параметров = пользователь сам сбросил
//    фильтры → ключ удаляется. Явный сброс — «?reset=1».
// 2) Страницы-инструменты (arm solve/find-error, регрессия промптов,
//    тестовая консоль): значения полей формы сохраняются per-pathname на
//    change/input и восстанавливаются на загрузке. Каскадные селекты
//    (расширение → темы → препромпты, наполняются асинхронными fetch)
//    применяются повторными проходами, пока значения не «прилипнут».
//    Страница с ?run_id= не восстанавливается — приоритет у серверного
//    восстановления из AIModelTestRun.run_params.
//    Поля с атрибутом data-no-state исключаются из сохранения/восстановления
//    (например, динамическое дерево задач /arm/solve/, у которого выделенный
//    механизм localStorage ai_arm_solve_nodes).
//
// request_logs.html держит собственную логику фильтров (localStorage
// ai_logs_filters) и не является changelist'ом — этот механизм её не задевает.
(function () {
  "use strict";

  var QS_KEY_PREFIX = "ai_admin_state:";
  var FORM_KEY_PREFIX = "ai_admin_form:";
  // Формы страниц-инструментов: pathname → селектор формы. Эти шаблоны НЕ
  // используют обёртку #content-main (в отличие от django changelist'ов),
  // поэтому ищем форму по её id явно. «Препромпты по умолчанию» здесь не
  // участвует: у него предзаполнение ?edit= и JS-каскад селектов —
  // generic-restore конфликтовал бы с ними (KISS: страница короткая).
  var FORM_SELECTORS = {
    "/ai/admin/arm/solve/": "#armSolveForm",
    "/ai/admin/arm/find-error/": "#armRunForm",
    "/ai/admin/prompt-regression/": "#prtRunForm",
    "/ai/admin/test-console/": "#tcRunForm"
  };
  var RETRY_MS = 1000;
  var MAX_RETRIES = 25; // дерево задач / каскадные селекты грузятся асинхронно

  function parseSaved(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || "null");
    } catch (e) {
      return null;
    }
  }

  // --- 1) Querystring changelist'ов ------------------------------------

  function setupChangelistState() {
    if (!document.getElementById("changelist")) return;
    var key = QS_KEY_PREFIX + window.location.pathname;
    var params = new URLSearchParams(window.location.search);

    if (params.get("reset")) {
      localStorage.removeItem(key);
      return;
    }
    if (window.location.search) {
      localStorage.setItem(key, window.location.search);
      return;
    }
    var saved = localStorage.getItem(key);
    if (!saved) return;
    var referrer = null;
    try {
      referrer = new URL(document.referrer);
    } catch (e) { /* пустой или некорректный referrer */ }
    var sameOrigin = referrer && referrer.origin === window.location.origin;
    if (sameOrigin && referrer.pathname !== window.location.pathname) {
      // Вернулись со страницы объекта/по ссылке меню — возвращаем фильтры.
      window.location.replace(window.location.pathname + saved);
    } else if (sameOrigin) {
      // Пришли с этой же страницы без querystring — фильтры сняли руками.
      localStorage.removeItem(key);
    }
  }

  // --- 2) Формы страниц-инструментов -----------------------------------

  function fieldKey(el) {
    if (el.id) return "#" + el.id;
    if (!el.name) return null;
    // Группа чекбоксов с одним name (модели на arm-страницах) различаем
    // значением: «models=<model_key>».
    if (el.type === "checkbox" || el.type === "radio") return el.name + "=" + el.value;
    return el.name;
  }

  function isSaveable(el) {
    var type = el.type || "";
    return !el.disabled
      && !el.hasAttribute("data-no-state")
      && fieldKey(el) !== null
      && type !== "file" && type !== "hidden" && type !== "password"
      && el.name !== "csrfmiddlewaretoken";
  }

  function setupFormState() {
    var form = document.querySelector(FORM_SELECTORS[window.location.pathname] || "");
    if (!form) return;
    var key = FORM_KEY_PREFIX + window.location.pathname;

    function collect() {
      var data = {};
      form.querySelectorAll("select, input, textarea").forEach(function (el) {
        if (!isSaveable(el)) return;
        data[fieldKey(el)] = el.type === "checkbox" ? el.checked : el.value;
      });
      return data;
    }

    function save() {
      try {
        localStorage.setItem(key, JSON.stringify(collect()));
      } catch (e) { /* quota — состояние не критично */ }
    }

    form.addEventListener("change", save, true);
    form.addEventListener("input", save, true);

    if (new URLSearchParams(window.location.search).has("run_id")) return;

    var savedState = parseSaved(key);
    if (!savedState) return;

    // Значения применяем проходами с повторным сканированием DOM: часть
    // полей появляется позже (чекбоксы задач дерева — только после loadTree,
    // options каскадных селектов — после асинхронных fetch). Применённые
    // ключи запоминаем, остальных ждём до MAX_RETRIES.
    var applied = {};

    function applyPass(attempt) {
      form.querySelectorAll("select, input, textarea").forEach(function (el) {
        if (!isSaveable(el)) return;
        var k = fieldKey(el);
        if (applied[k]) return;
        if (!Object.prototype.hasOwnProperty.call(savedState, k)) {
          applied[k] = true; // отсутствовало в сохранённом — не трогаем
          return;
        }
        var value = savedState[k];
        if (el.type === "checkbox") {
          if (el.checked !== !!value) {
            el.checked = !!value;
            el.dispatchEvent(new Event("change"));
          }
          applied[k] = true;
        } else if (el.tagName === "SELECT") {
          var exists = Array.prototype.some.call(
            el.options, function (o) { return o.value === String(value); }
          );
          if (exists) {
            if (el.value !== String(value)) {
              el.value = String(value);
              el.dispatchEvent(new Event("change"));
            }
            applied[k] = true;
          }
        } else {
          if (el.value !== value) {
            el.value = value;
            el.dispatchEvent(new Event("change"));
          }
          applied[k] = true;
        }
      });
      var pendingCount = 0;
      form.querySelectorAll("select, input, textarea").forEach(function (el) {
        if (isSaveable(el) && !applied[fieldKey(el)]) pendingCount++;
      });
      if (pendingCount && attempt < MAX_RETRIES) {
        window.setTimeout(function () { applyPass(attempt + 1); }, RETRY_MS);
      }
    }

    applyPass(0);
  }

  setupChangelistState();
  setupFormState();
})();