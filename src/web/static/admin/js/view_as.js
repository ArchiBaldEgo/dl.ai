/* Режим «Посмотреть как…» (ai/admin/guest_mode.py) — окно выбора группы.
 *
 * Кнопка #aiViewAsToggle в шапке админки (только реальные суперюзеры —
 * флаг real_super_user рендерит её на сервере). Клик — fetch списка групп
 * (GET ai_view_as_groups: только группы, у участников которых есть page.admin —
 * остальные в админку всё равно не входят), затем <dialog> с построчным
 * выбором. Выбор — POST ai_view_as_select {group_id} (0 = «Полная админка»),
 * после — перезагрузка: админка уже нарисуется честным правам группы.
 *
 * ВАЖНО (честность): эмуляция отключает супер-обход полностью — доступные
 * страницы считаются только по ключам группы; недоступное даёт 403
 * или форму логина, ровно как у участника.
 */
(function () {
  "use strict";

  var TOGGLE_ID = "aiViewAsToggle";
  var GROUPS_URL = "/ai/admin/view-as/groups/";
  var SELECT_URL = "/ai/admin/view-as/select/";

  function csrfToken() {
    // Прод переименовывает CSRF-куку (CSRF_COOKIE_NAME=ai_csrftoken) —
    // читаем по ХВОСТУ имени.
    var pairs = document.cookie.split(/;\s*/);
    for (var i = 0; i < pairs.length; i++) {
      var idx = pairs[i].indexOf("=");
      if (idx <= 0) continue;
      var name = pairs[i].slice(0, idx);
      if (name.length >= "csrftoken".length &&
          name.slice(-"csrftoken".length) === "csrftoken") {
        return pairs[i].slice(idx + 1);
      }
    }
    return "";
  }

  function esc(text) {
    var d = document.createElement("div");
    d.textContent = text;  // рендер только textContent — без innerHTML инъекций
    return d;
  }

  function openDialog() {
    return fetch(GROUPS_URL, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok) { throw new Error("no groups"); }
        return data.groups;
      })
      .then(function (groups) {
        var dlg = document.createElement("dialog");
        dlg.className = "ai-view-as-dialog";
        var h = document.createElement("p");
        h.textContent = "Посмотреть как участник группы:";
        dlg.appendChild(h);
        var list = document.createElement("ul");
        list.className = "ai-view-as-list";
        groups.forEach(function (g) {
          var li = document.createElement("li");
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "button ai-view-as-pick";
          btn.textContent = g.name + (g.current ? " (текущий режим)" : "");
          btn.dataset.groupId = g.id;
          li.appendChild(btn);
          list.appendChild(li);
        });
        dlg.appendChild(list);
        var exit = document.createElement("button");
        exit.type = "button";
        exit.className = "button ai-view-as-exit";
        exit.textContent = "Полная админка (выйти из режима)";
        dlg.appendChild(exit);
        var close = document.createElement("button");
        close.type = "button";
        close.className = "button ai-view-as-close";
        close.textContent = "Отмена";
        dlg.appendChild(close);
        document.body.appendChild(dlg);
        if (typeof dlg.showModal !== "function") { dlg.setAttribute("open", ""); }
        else { dlg.showModal(); }

        dlg.addEventListener("click", function (e) {
          var pick = e.target.closest("button");
          if (!pick) return;
          if (pick.classList.contains("ai-view-as-close")) {
            dlg.close();
            dlg.remove();
            return;
          }
          if (!pick.classList.contains("ai-view-as-exit") &&
              !pick.classList.contains("ai-view-as-pick")) {
            return;
          }
          var groupId = pick.classList.contains("ai-view-as-pick")
            ? pick.dataset.groupId : "0";
          var body = new FormData();
          body.append("group_id", groupId);
          body.append("csrfmiddlewaretoken", csrfToken());
          fetch(SELECT_URL, {
            method: "POST",
            credentials: "same-origin",
            body: body,
          }).then(function (r) {
            if (r.ok) { window.location.href = "/ai/admin/"; return null; }
            return r.json();
          }).then(function (data) {
            // Не-ok (протухшая сессия/не супер) — честный текст, не молчание:
            // AJAX редирект при ошибке не уводит пользователя.
            alert((data && data.message) ||
              "Не удалось переключить режим — обновите страницу (Ctrl+F5) и повторите");
          }).catch(function () { /* форма логина при протухшей сессии — редирект уже ушёл */ });
        });
      })
      .catch(function () {
        // Сессия протухла/не супер: тихо ничего не делаем.
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById(TOGGLE_ID);
    if (!btn) return;
    btn.addEventListener("click", openDialog);
  });
})();