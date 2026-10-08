/**
 * ai-wizard.js — онбординг-тур (spotlight) для пользовательских страниц
 * и страниц админки. Vanilla ES6, без зависимостей и сборки.
 *
 * Решение «показывать ли тур» принимает сервер (ai/services/onboarding.py):
 * конфиг приходит в <script id="ai-wizard-config"> через json_script. При
 * show:false скрипт молчит — но делегированный слушатель кликов ставится
 * всегда, чтобы «Подсказки» (#aiDocsTourBtn) и [data-ai-wizard-restart]
 * могли показать тур повторно.
 *
 * Слои (.ai-wizard-blocker/.ai-wizard-spot/.ai-wizard-popover) вешаются на
 * document.body, стили — static/admin/css/ai-wizard.css.
 * Рендер только createElement + textContent (CLAUDE.md: никакого innerHTML
 * с данными). document.body.style.overflow не трогаем: страница продолжает
 * жить/скроллиться под туром, пятно едет за прокруткой.
 *
 * Экспорт: window.AIWizard = { start, stop, isActive }.
 * (window.onload НЕ используем — пользовательские страницы сами его выставляют.)
 */
(function () {
    "use strict";

    // === Константы раскладки ===
    var CONFIG_ID = "ai-wizard-config";
    var SPOT_PAD = 6;        // «подушка» вокруг подсвеченного элемента
    var POPOVER_GAP = 10;    // зазор между пятном и поповером
    var VIEW_MARGIN = 8;     // гарантированный отступ от краёв окна
    var ARROW_INSET = 20;    // ромб стрелки не прижимается к краям поповера
    var ARROW_SIZE = 12;
    var BIG_RATIO = 0.6;     // элемент выше 60% окна — подсвечиваем средние 60%
    var RECHECK_MS = 400;    // интервал пере-замера (живой DOM чата/меню)
    var START_DELAY_MS = 400; // пауза после boot: даём init-кодам страниц отработать
    var SHEET_QUERY = "(max-width: 640px)";

    var POP_SHEET = "ai-wizard-popover--sheet";
    var POP_ENTER = "ai-wizard-popover--enter";
    var ARROW_BASE = "ai-wizard-arrow";
    var ARROW_OFF = "ai-wizard-arrow--hidden";
    // Класс стрелки — кромка ПОПОВЕРА, на которой ромб (к точке).
    var ARROW_EDGE = { bottom: "top", top: "bottom", right: "left", left: "right" };

    var BTN_BASE = "ai-wizard-btn";
    var BTN_PREV = BTN_BASE + " ai-wizard-btn-prev";
    var BTN_NEXT = BTN_BASE + " ai-wizard-btn-next";
    var BTN_DONE = BTN_BASE + " ai-wizard-btn-done";
    var BTN_SKIP = BTN_BASE + " ai-wizard-btn-skip";

    var LABEL_PREV = "Назад";
    var LABEL_NEXT = "Далее";
    var LABEL_DONE = "Готово";
    var LABEL_SKIP = "Скрыть";

    // === Реестр шагов (тексты — константы языка: правятся только здесь) ===
    var REGISTRY = {

        chat: [
            { sel: "#selectLang", title: "Язык страницы", text: "Тут меняется язык надписей: Русский, English, Français.", pos: "bottom" },
            { sel: "#select", title: "Выбор модели", text: "Это разные помощники-модели. Нажми и выбери, кто будет тебе отвечать.", pos: "bottom" },
            { sel: "#selectType", title: "Другие страницы", text: "Через этот список можно уйти на страницы «Реши задачу» и «В чём ошибка».", pos: "bottom" },
            { sel: "#voiceModeBtn", title: "Голосовой режим", text: "Хочешь говорить, а не печатать? Нажми — можно говорить голосом.", pos: "bottom" },
            { sel: "#themeToggleBtn", title: "Светлое или тёмное", text: "Нажми — страница станет тёмной. Ещё раз — снова светлой.", pos: "bottom" },
            { sel: "#userDocsBtn", title: "Кнопка «?»", text: "Это инструкция. Нажми, если что-то непонятно. Там же можно снова показать эти подсказки.", pos: "bottom" },
            { sel: "#messages", title: "Здесь появляются ответы", text: "Всё, что ты пишешь и что отвечает модель, видно здесь.", pos: "center" },
            { sel: "#messageText", title: "Здесь пишешь вопрос", text: "Просто печатай свой вопрос в это поле.", pos: "top" },
            { sel: '.buttons-block button[type="submit"]', title: "Кнопка «Отправить»", text: "Написал вопрос — жми эту кнопку.", pos: "top" },
            { sel: '[onclick="clearContext()"]', title: "Стереть всё", text: "Эта кнопка стирает переписку и начинает сначала.", pos: "top", optional: true },
            { sel: "#content .toggle-button", title: "Боковое меню", text: "Стрелочка справа открывает меню, там ссылка на админ-панель.", pos: "left", optional: true }
        ],

        solve: [
            { sel: "#selectType", title: "Где ты", text: "Ты на странице «Реши задачу». Через этот список вернёшься в «Чат».", pos: "bottom" },
            { sel: "#selectLang", title: "Язык страницы", text: "Тут меняется язык надписей.", pos: "bottom" },
            { sel: "#select", title: "Выбор модели", text: "Список моделей — помощников, которые будут решать задачу.", pos: "bottom" },
            { sel: "#selectProgLng", title: "Язык программирования", text: "Сначала выбери язык твоего кода: C или Ассемблер.", pos: "bottom" },
            { sel: "#selectTheme", title: "Тема", text: "Выбери тему — она объясняет модели, о чём задача.", pos: "top" },
            { sel: "#selectPrompt", title: "Препромпт", text: "Препромпт — маленькая подсказка для модели. Хватит того, что выбрано.", pos: "top" },
            { sel: "#messages", title: "Тут придёт решение", text: "Готовое решение появится в этом окне.", pos: "center" },
            { sel: "#messageText", title: "Поле задания", text: "Опиши здесь задачу или то, что не получается.", pos: "top" },
            { sel: '.buttons-block button[type="submit"]', title: "Отправить", text: "Жми — и жди решение в окне чата.", pos: "top" },
            { sel: "#userDocsBtn", title: "Кнопка «?»", text: "Инструкция. Там же снова показываются эти подсказки.", pos: "bottom" },
            { sel: '[onclick="clearContext()"]', title: "Стереть всё", text: "Стирает переписку и начинает сначала.", pos: "top", optional: true },
            { sel: "#testOnDlBtn", title: "Тестирование в DL", text: "Отправит решение на проверку в dl.gsu.by. Кнопка видна, когда задача пришла с номером из DL.", pos: "top", optional: true },
            { sel: "#selectModelSort", title: "Сортировка моделей", text: "Можно показывать более быстрые или более точные модели сверху.", pos: "bottom", optional: true }
        ],

        find_error: [
            { sel: "#selectType", title: "Где ты", text: "Ты на странице «В чём ошибка». Через список вернёшься в «Чат» или в «Реши задачу».", pos: "bottom" },
            { sel: "#selectLang", title: "Язык страницы", text: "Тут меняется язык надписи.", pos: "bottom" },
            { sel: "#select", title: "Выбор модели", text: "Выбери помощника-модель, которая проверит код.", pos: "bottom" },
            { sel: "#selectProgLng", title: "Язык программирования", text: "Выбери язык, на котором написан код.", pos: "top" },
            { sel: "#selectTheme", title: "Тема", text: "Тема помогает модели понять, о чём задача.", pos: "top" },
            { sel: "#selectPrompt", title: "Препромпт", text: "Подсказка для модели; хватает того, что выбрано.", pos: "top" },
            { sel: "#taskText", title: "Условие задачи", text: "Скопируй сюда условие задачи из dl.gsu.by.", pos: "top" },
            { sel: "#codeText", title: "Твой код", text: "А сюда вставь программу целиком.", pos: "top" },
            { sel: '.buttons-block button[type="submit"]', title: "Отправить", text: "Жми — модель покажет, что не так в коде.", pos: "top" },
            { sel: "#messages", title: "Тут будет ответ", text: "Ответ модели появится в этом окне.", pos: "center" },
            { sel: "#userDocsBtn", title: "Кнопка «?»", text: "Инструкция и повторный показ подсказок.", pos: "bottom", optional: true }
        ],

        admin: [
            { sel: "#toggle-nav-sidebar", title: "Край страницы", text: "Тонкая кнопка у левого края: открывает и закрывает меню разделов.", pos: "right" },
            { sel: "#nav-sidebar", title: "Меню разделов", text: "Здесь все разделы админки. Нажми на нужный.", pos: "right", onShow: "openNav" },
            { sel: '#nav-sidebar a[href="/ai/admin/arm/solve/"]', title: "Пакетное решение", text: "Тут запускают проверку моделей сразу на всех задачах курса.", pos: "right" },
            { sel: '#nav-sidebar a[href="/ai/admin/arm/models/"]', title: "Состояние моделей", text: "Какие модели работают, а какие нет. Видно только админам.", pos: "right" },
            { sel: '#nav-sidebar a[href="/ai/admin/docs/prompt-developer/"]', title: "Документация", text: "Инструкции лежат здесь. Начни с «Разработчика промптов».", pos: "right" },
            { sel: "#aiProcessesToggle", title: "Приветствие", text: "Это ты: имя и роль. Рядом счётчик «Процессы» — твои запуски.", pos: "bottom" },
            { sel: "#aiProcessesMenu", title: "Меню «Процессы»", text: "Здесь видно свои запуски: что идёт и чем закончилось.", pos: "left", onShow: "openProcesses" },
            { sel: "#nav-filter", title: "Поиск по меню", text: "Разделов много? Набери пару букв — останется нужное.", pos: "right", optional: true },
            { sel: "#aiGuestToggle", title: "Посмотреть как другой", text: "Суперадмин может открыть админку глазами гостя. Повторное нажатие вернёт всё назад.", pos: "bottom" },
            { sel: ".ai-role-badge", title: "Твоя роль", text: "Здесь подписано, кто ты: разработчик промптов, админ или суперадмин.", pos: "bottom", optional: true }
        ],

        admin_arm_solve: [
            { sel: "#armCourseId", title: "Номер курса", text: "Это номер курса на dl.gsu.by. Он уже вписан — не меняй без нужды.", pos: "right" },
            { sel: "#armLoadTreeBtn", title: "Загрузить задачи", text: "Нажми — подтянутся задачи курса из dl.gsu.by.", pos: "right" },
            { sel: "#armTreeContainer", title: "Дерево задач", text: "Отметь задачи, на которых проверить модели.", pos: "right", optional: true },
            { sel: "#armTreeSelectAll", title: "Выбрать всё", text: "Отметит все задачи разом. Появляется после загрузки.", pos: "right", optional: true },
            { sel: "#armSelectAllModels", title: "Модели", text: "Отметь модели, которые будут решать. Ничего не отмечено — значит все.", pos: "bottom", optional: true },
            { sel: "#armLanguageChecks", title: "Языки", text: "Выбери один или несколько языков: на каждый запустится свой процесс.", pos: "bottom", optional: true },
            { sel: "#armExtensionInput", title: "Расширение", text: "Расширение файла определяется само, из выбранных языков.", pos: "right" },
            { sel: "#armTopicSelect", title: "Тема", text: "Станет доступной после выбора языка.", pos: "right", optional: true },
            { sel: "#armPromptSelect", title: "Препромпт", text: "Обычно «По привязке» — подставится препромпт по умолчанию. Можно выбрать свой.", pos: "right" },
            { sel: "#armSaveSolutions", title: "Кэш решений", text: "Если задача прошла тест в DL, её решение сохранится — потом достанется без вызова модели.", pos: "right" },
            { sel: "#armRecordStats", title: "Статистика", text: "Суперадмин может заносить результаты в базу — они попадут в селектор моделей чата.", pos: "right", optional: true },
            { sel: "#armRunName", title: "Название прогона", text: "Придумай короткое имя — так прогон потом легко найти в журнале.", pos: "right" },
            { sel: ".arm-warning", title: "Важно", text: "Пока идёт прогон, лучше не уходить со страницы — тестирование прервётся.", pos: "bottom", optional: true },
            { sel: "#armSolveSubmit", title: "Запустить", text: "Всё выбрал — жми эту кнопку. Прогон появится в «Процессах» в шапке.", pos: "top" },
            { sel: "#armSolveProcessesCard", title: "Процессы", text: "Здесь список запущенных прогонов. Оттуда он открывается в таблицу результатов.", pos: "top", optional: true }
        ],

        admin_model_status: [
            { sel: "#statusHealthWindowDate", title: "Окно проверки", text: "Модели проверяются раз в сутки в 04:00. Дата последнего окна — здесь.", pos: "bottom" },
            { sel: "#refreshModelsBtn", title: "Обновить сейчас", text: "Не хочешь ждать ночи — нажми, и модели проверятся сразу.", pos: "bottom" },
            { sel: "#refreshInProgressHint", title: "Идёт проверка", text: "Пока работает проверка, страница обновляется сама.", pos: "right", optional: true },
            { sel: "#modelStatusRowsBody", title: "Список моделей", text: "«Активна» — работает; «Неактивна» — надо разбираться. В таблице время, код и расшифровка.", pos: "top", optional: true }
        ]
    };

    // === Состояние тура ===
    var active = false;
    var cfg = null;            // конфиг активации (scope/role/mark_url)
    var steps = [];            // шаги, прошедшие фильтр роли и наличия
    var index = -1;

    var blocker = null;
    var spot = null;
    var popover = null;
    var arrow = null;
    var parts = {};            // title/text/progress + кнопки

    var pollTimer = null;      // setInterval(RECHECK_MS)
    var bootTimer = null;      // отложенный автостарт после boot()
    var marked = false;        // отметку «seen» шлём один раз за активацию
    var runSeq = 0;            // номер активации: rAF с прошлого тура отбрасываем

    // Восстановление хуками изменённого состояния админ-хрома.
    var navOpenedByTour = false;   // тур открыл закрытую левую навигацию
    var procOpenedByTour = false;  // тур открыл меню «Процессы»

    // === Мелкие хелперы ===

    function querySel(sel) {
        try {
            return document.querySelector(sel);
        } catch (e) {
            return null; // некорректный селектор шага — шаг пропустится
        }
    }

    // «Видим» = есть layout-размер (offset* или rect). display:none/hidden -> нет.
    function isVisible(el) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        return Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
            (r.width > 0 || r.height > 0);
    }

    function clampNum(v, lo, hi) {
        return Math.max(lo, Math.min(v, hi));
    }

    function getCsrfToken() {
        var cookies = document.cookie.split(";");
        for (var i = 0; i < cookies.length; i++) {
            var parts_ = cookies[i].trim().split("=");
            if (parts_[0] === "csrftoken") return parts_[1];
        }
        return "";
    }

    // === Слои тура ===

    function ensureLayers() {
        if (blocker) return;

        blocker = document.createElement("div");
        blocker.className = "ai-wizard-blocker";
        // Приёмник кликов: ткнуть мимо поповера в туре ничего не делает,
        // закрывается тур только Скрыть/Готово/Escape. Mousedown тоже глушим:
        // страницы вешают на него свои реакции (напр. ai_processes.js закрывает
        // меню «Процессы»), которые тур не должен дёргать. Скролл намеренно НЕ
        // блокируем — страница продолжает листаться под туром, пятно следует.
        blocker.addEventListener("mousedown", function (event) {
            event.preventDefault();
            event.stopPropagation();
        });
        blocker.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
        });

        spot = document.createElement("div");
        spot.className = "ai-wizard-spot";
        spot.setAttribute("aria-hidden", "true");
        spot.style.display = "none";

        popover = document.createElement("div");
        popover.className = "ai-wizard-popover";
        popover.setAttribute("role", "dialog");
        popover.setAttribute("aria-label", "Онбординг-подсказка");
        popover.setAttribute("tabindex", "-1");

        parts.title = document.createElement("h3");
        parts.title.className = "ai-wizard-step-title";
        parts.text = document.createElement("p");
        parts.text.className = "ai-wizard-step-text";
        parts.progress = document.createElement("div");
        parts.progress.className = "ai-wizard-progress";

        var footer = document.createElement("div");
        footer.className = "ai-wizard-footer";

        parts.btnSkip = document.createElement("button");
        parts.btnSkip.type = "button";
        parts.btnSkip.className = BTN_SKIP;
        parts.btnSkip.textContent = LABEL_SKIP;
        parts.btnSkip.addEventListener("click", function () {
            stop({ mark: true });
        });

        parts.btnPrev = document.createElement("button");
        parts.btnPrev.type = "button";
        parts.btnPrev.className = BTN_PREV;
        parts.btnPrev.textContent = LABEL_PREV;
        parts.btnPrev.addEventListener("click", function () {
            prev();
        });

        parts.btnNext = document.createElement("button");
        parts.btnNext.type = "button";
        parts.btnNext.className = BTN_NEXT;
        parts.btnNext.textContent = LABEL_NEXT;
        parts.btnNext.addEventListener("click", function () {
            next();
        });

        arrow = document.createElement("div");
        arrow.className = ARROW_BASE + " " + ARROW_OFF;

        footer.appendChild(parts.btnSkip);
        footer.appendChild(parts.btnPrev);
        footer.appendChild(parts.btnNext);
        popover.appendChild(parts.title);
        popover.appendChild(parts.text);
        popover.appendChild(parts.progress);
        popover.appendChild(footer);
        popover.appendChild(arrow);

        document.body.appendChild(blocker);
        document.body.appendChild(spot);
        document.body.appendChild(popover);
    }

    function removeLayers() {
        if (blocker && blocker.parentNode) blocker.parentNode.removeChild(blocker);
        if (spot && spot.parentNode) spot.parentNode.removeChild(spot);
        if (popover && popover.parentNode) popover.parentNode.removeChild(popover);
        blocker = null;
        spot = null;
        popover = null;
        arrow = null;
        parts = {};
    }

    // === Шаги ===

    // Фильтр реестра: (а) роль — шаг с roles[] участвует, только если список
    // пуст или содержит cfg.role (на пользовательских страницах role="",
    // поэтому участвуют только шаги без roles); (б) наличие цели: элемент есть
    // и виден. Для шагов с onShow видимость пока не требуем — хук сам покажет
    // цель (напр. #aiProcessesMenu скрыт до openProcesses).
    function resolveSteps(scope, role) {
        var list = REGISTRY[scope];
        var out = [];
        if (!list || !list.length) return out;
        for (var i = 0; i < list.length; i++) {
            var s = list[i];
            if (s.roles && s.roles.length) {
                if (s.roles.indexOf(role) === -1) continue;
            }
            if (s.onShow) {
                if (!querySel(s.sel)) continue;
            } else {
                if (!isVisible(querySel(s.sel))) continue;
            }
            out.push(s);
        }
        return out;
    }

    function runHook(name) {
        var hook = HOOKS[name];
        if (!hook) return;
        try {
            hook();
        } catch (e) {
            // Хук упал (нет нужного админ-хрома) — шаг просто идёт дальше.
        }
    }

    function show(i) {
        if (!active) return;
        if (i < 0 || i >= steps.length) {
            stop({ mark: true });
            return;
        }
        index = i;
        var step = steps[i];
        // Сначала хук (он может открыть навигацию/меню под целью), затем
        // скроллим и меряем в следующем кадре.
        if (step.onShow) runHook(step.onShow);

        var el = querySel(step.sel);
        if (!el || !isVisible(el)) {
            skipForward();
            return;
        }
        try {
            el.scrollIntoView({ block: "center", behavior: "instant" });
        } catch (e) {
            try {
                el.scrollIntoView();
            } catch (e2) { /* без скролла — пятно всё равно рисуется */ }
        }
        // Замер и позиция — в следующем кадре (после scrollIntoView); кадры
        // с прошлого тура отбрасываем по номеру активации.
        var seqAtCall = runSeq;
        requestAnimationFrame(function () {
            if (!active || runSeq !== seqAtCall) return;
            renderContent(step);
            place();
        });
    }

    // Шаг сам исчез (перерисовка DOM): идём дальше, на последнем — финиш.
    function skipForward() {
        if (index + 1 < steps.length) {
            show(index + 1);
        } else {
            stop({ mark: true });
        }
    }

    function next() {
        if (!active) return;
        if (index + 1 < steps.length) {
            show(index + 1);
        } else {
            stop({ mark: true });
        }
    }

    function prev() {
        if (!active) return;
        if (index > 0) show(index - 1);
    }

    function renderContent(step) {
        parts.title.textContent = step.title || "";
        parts.text.textContent = step.text || "";
        parts.progress.textContent = (index + 1) + " из " + steps.length;
        parts.btnPrev.disabled = index === 0;
        var last = index === steps.length - 1;
        parts.btnNext.textContent = last ? LABEL_DONE : LABEL_NEXT;
        parts.btnNext.className = last ? BTN_DONE : BTN_NEXT;
        // Вхождение — заново на каждом шаге (subtle).
        popover.classList.remove(POP_ENTER);
        void popover.offsetWidth; // reflow, чтобы анимация переигралась
        popover.classList.add(POP_ENTER);
        popover.focus({ preventScroll: true });
    }

    // === Позиционирование ===

    // Элемент выше 60% окна (напр. #messages) — подсвечиваем средние 60%.
    function shrinkBigRect(r, viewH) {
        var keep = viewH * BIG_RATIO;
        var cut = (r.height - keep) / 2;
        return {
            left: r.left,
            right: r.right,
            width: r.width,
            top: r.top + cut,
            bottom: r.bottom - cut,
            height: r.height - cut * 2
        };
    }

    // Пятно не должно вылезать в невидимую зону (скроллбар/за край окна).
    function clampToViewport(r, viewW, viewH) {
        var left = Math.max(0, r.left);
        var top = Math.max(0, r.top);
        var right = Math.min(viewW, r.right);
        var bottom = Math.min(viewH, r.bottom);
        if (right - left < 2 || bottom - top < 2) return null; // целиком за кадром
        return {
            left: left, top: top, right: right, bottom: bottom,
            width: right - left, height: bottom - top
        };
    }

    function sheetMode() {
        var viewW = document.documentElement ? document.documentElement.clientWidth : window.innerWidth;
        if (viewW <= 640) return true;
        try {
            if (window.matchMedia && window.matchMedia(SHEET_QUERY).matches) return true;
        } catch (e) { /* ниже порога уже поймали по ширине */ }
        return false;
    }

    // Порядок обхода сторон: запрошенная, противоположная, затем остальные.
    function sideOrder(pos) {
        var all = ["bottom", "top", "right", "left"];
        var i = all.indexOf(pos);
        if (i === -1) return [];
        var opp = all[i ^ 1]; // bottom<->top, right<->left
        var rest = [];
        for (var j = 0; j < all.length; j++) {
            if (all[j] !== pos && all[j] !== opp) rest.push(all[j]);
        }
        return [pos, opp].concat(rest);
    }

    function sideFits(side, r, pw, ph, viewW, viewH) {
        if (!r) return false;
        if (side === "bottom") return r.bottom + POPOVER_GAP + ph <= viewH - VIEW_MARGIN;
        if (side === "top") return r.top - POPOVER_GAP - ph >= VIEW_MARGIN;
        if (side === "right") return r.right + POPOVER_GAP + pw <= viewW - VIEW_MARGIN;
        if (side === "left") return r.left - POPOVER_GAP - pw >= VIEW_MARGIN;
        return false;
    }

    // Мобильный (или тесный) случай: нижний «лист» — позиционирует CSS,
    // инлайновые оффсеты сбрасываем; пятно рисуется как обычно.
    function showAsSheet() {
        arrow.className = ARROW_BASE + " " + ARROW_OFF;
        popover.classList.add(POP_SHEET);
        popover.style.left = "";
        popover.style.top = "";
    }

    function place() {
        if (!active || index < 0 || !steps[index]) return;
        var step = steps[index];

        var root = document.documentElement;
        var viewW = root ? root.clientWidth : window.innerWidth;
        var viewH = root ? root.clientHeight : window.innerHeight;

        var r = null;
        var el = querySel(step.sel);
        if (el && isVisible(el)) {
            var rect = el.getBoundingClientRect();
            if (rect.height > viewH * BIG_RATIO) rect = shrinkBigRect(rect, viewH);
            r = clampToViewport(rect, viewW, viewH);
        }
        if (r) {
            spot.style.display = "";
            spot.style.left = Math.round(r.left - SPOT_PAD) + "px";
            spot.style.top = Math.round(r.top - SPOT_PAD) + "px";
            spot.style.width = Math.round(r.width + SPOT_PAD * 2) + "px";
            spot.style.height = Math.round(r.height + SPOT_PAD * 2) + "px";
        } else {
            spot.style.display = "none";
        }

        placePopover(step, r, viewW, viewH);
    }

    function placePopover(step, r, viewW, viewH) {
        // Меряем «натуральный» размер: без sheet-класса и с чистыми оффсетами
        // прошлого шага. Всё синхронно до конца тика — вспышки нет.
        popover.classList.remove(POP_SHEET);
        popover.style.left = "";
        popover.style.top = "";
        arrow.className = ARROW_BASE + " " + ARROW_OFF;
        var pw = popover.offsetWidth;
        var ph = popover.offsetHeight;

        if (sheetMode()) {
            showAsSheet();
            return;
        }

        var wanted = step.pos === "center" ? "center" : (step.pos || "bottom");
        var chosen = null;
        if (wanted === "center" || !r) {
            // Пятна нет (цель за кадром) — поповер по центру, без стрелки.
            chosen = "center";
        } else {
            var order = sideOrder(wanted);
            for (var i = 0; i < order.length; i++) {
                if (sideFits(order[i], r, pw, ph, viewW, viewH)) {
                    chosen = order[i];
                    break;
                }
            }
        }

        if (!chosen) {
            // Ни у одного борта нет места — нижний «лист».
            showAsSheet();
            return;
        }

        if (chosen === "center") {
            popover.style.left = Math.max(VIEW_MARGIN, Math.round((viewW - pw) / 2)) + "px";
            popover.style.top = Math.max(VIEW_MARGIN, Math.round((viewH - ph) / 2)) + "px";
            return;
        }

        var scx = r.left + r.width / 2;
        var scy = r.top + r.height / 2;
        var left, top;
        if (chosen === "bottom" || chosen === "top") {
            top = chosen === "bottom" ? r.bottom + POPOVER_GAP : r.top - POPOVER_GAP - ph;
            left = Math.max(VIEW_MARGIN, Math.min(scx - pw / 2, viewW - VIEW_MARGIN - pw));
        } else {
            left = chosen === "right" ? r.right + POPOVER_GAP : r.left - POPOVER_GAP - pw;
            top = Math.max(VIEW_MARGIN, Math.min(scy - ph / 2, viewH - VIEW_MARGIN - ph));
        }
        popover.style.left = Math.round(left) + "px";
        popover.style.top = Math.round(top) + "px";

        // Стрелка-ромб на кромке поповера к точке, вдоль кромки — на центр цели.
        var edge = ARROW_EDGE[chosen];
        arrow.className = ARROW_BASE + " ai-wizard-arrow--" + edge;
        if (edge === "top" || edge === "bottom") {
            var ax = clampNum(scx - left, ARROW_INSET, pw - ARROW_INSET);
            arrow.style.top = "";
            arrow.style.left = Math.round(ax - ARROW_SIZE / 2) + "px";
        } else {
            var ay = clampNum(scy - top, ARROW_INSET, ph - ARROW_INSET);
            arrow.style.left = "";
            arrow.style.top = Math.round(ay - ARROW_SIZE / 2) + "px";
        }
    }

    // === Пере-замер: скролл (в т.ч. внутренних скроллеров), resize, тик ===

    function onScroll() {
        if (!active) return;
        place();
    }

    function tick() {
        if (!active) return;
        var step = steps[index];
        if (!step) return;
        // Цель исчезла из DOM или скрылась — шаг пропускаем.
        if (!isVisible(querySel(step.sel))) {
            skipForward();
            return;
        }
        place();
    }

    // === Клавиатура (пока тур активен) ===

    function onKeydown(event) {
        if (!active) return;
        var key = event.key;
        if (key === "Escape") {
            // Во время тура Escape принадлежит туру (страница его не получает).
            event.preventDefault();
            stop({ mark: true });
            return;
        }
        var t = event.target;
        var tag = t && t.tagName ? String(t.tagName).toUpperCase() : "";
        // Не мешаем печатать в полях под туром (если фокус там остался).
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        // Enter по кнопке поповера обработает сам click — не дублируем.
        if (key === "Enter" && tag === "BUTTON" && popover && popover.contains(t)) return;
        if (key === "ArrowRight" || key === "Enter") {
            event.preventDefault();
            next();
        } else if (key === "ArrowLeft") {
            event.preventDefault();
            prev();
        }
    }

    // === Хуки шагов (знают админ-DOM) ===

    var HOOKS = {
        // Открыть левую навигацию админки (stock nav_sidebar.js: #main.shifted,
        // aria-expanded + localStorage — он читает ключ только при загрузке,
        // поэтому выставляем все три). Состояние до тура вернём в stop().
        openNav: function () {
            var main = document.getElementById("main");
            if (!main) return;
            if (!main.classList.contains("shifted")) navOpenedByTour = true;
            try {
                localStorage.setItem("django.admin.navSidebarIsOpen", "true");
            } catch (e) { /* приватный режим — не критично */ }
            main.classList.add("shifted");
            var nav = document.getElementById("nav-sidebar");
            if (nav) nav.setAttribute("aria-expanded", "true");
        },

        // Меню «Процессы» в шапке: click по штатному toggle (ai_processes.js).
        // Уже открыто — кликом НЕ трогаем (иначе закрыли бы) и не «своё».
        openProcesses: function () {
            var toggle = document.getElementById("aiProcessesToggle");
            var menu = document.getElementById("aiProcessesMenu");
            if (!toggle) return;
            if (menu && !menu.hidden) return;
            procOpenedByTour = true;
            try {
                toggle.click();
            } catch (e) { /* меню не откроется — шаг пропустится по тику */ }
        }
    };

    function restoreState() {
        if (navOpenedByTour) {
            var main = document.getElementById("main");
            var nav = document.getElementById("nav-sidebar");
            try {
                localStorage.setItem("django.admin.navSidebarIsOpen", "false");
            } catch (e) {}
            if (main) main.classList.remove("shifted");
            if (nav) nav.setAttribute("aria-expanded", "false");
            navOpenedByTour = false;
        }
        if (procOpenedByTour) {
            var toggle = document.getElementById("aiProcessesToggle");
            var menu = document.getElementById("aiProcessesMenu");
            if (toggle && menu && !menu.hidden) {
                try {
                    toggle.click();
                } catch (e) {}
            }
            procOpenedByTour = false;
        }
    }

    // === Отметка «просмотрено» ===

    function markSeen() {
        if (marked || !cfg || !cfg.mark_url) return;
        marked = true;
        var body;
        try {
            body = JSON.stringify({ scope: String(cfg.scope || "") });
        } catch (e) {
            return;
        }
        var options = {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRFToken": getCsrfToken(),
                "Content-Type": "application/json"
            },
            body: body
        };
        try {
            fetch(cfg.mark_url, options)
                .then(function (resp) {
                    var type = resp && resp.headers
                        ? String(resp.headers.get("content-type") || "")
                        : "";
                    if (resp && resp.ok && type.indexOf("application/json") !== -1) return;
                    // Протухшая админ-сессия даёт HTML-редирект, не OK, не JSON:
                    // тихо игнорируем, одной строкой debug.
                    console.debug("[ai-wizard] seen-mark answer ignored (not ok / non-JSON)");
                })
                .catch(function () {
                    console.debug("[ai-wizard] seen-mark request failed (network)");
                });
        } catch (e) {
            console.debug("[ai-wizard] seen-mark not sent");
        }
    }

    // === API ===

    function start(scope, cfgArg) {
        if (active) return; // повторный start активного тура — no-op

        var conf = cfgArg || {};
        var scopeName = String(scope || conf.scope || "");

        var resolved = resolveSteps(scopeName, conf.role ? String(conf.role) : "");
        if (!resolved.length) return; // нечего показывать — тихо выходим

        steps = resolved;
        cfg = {
            scope: scopeName,
            version: conf.version,
            role: conf.role ? String(conf.role) : "",
            mark_url: conf.mark_url ? String(conf.mark_url) : ""
        };
        marked = false;
        navOpenedByTour = false;
        procOpenedByTour = false;
        active = true;
        runSeq++;

        ensureLayers();
        document.addEventListener("scroll", onScroll, true);
        window.addEventListener("resize", onScroll);
        document.addEventListener("keydown", onKeydown);
        pollTimer = setInterval(tick, RECHECK_MS);

        show(0);
    }

    function stop(opts) {
        if (!active) return; // нечего останавливать (отметку не шлём)
        active = false;
        runSeq++;
        clearInterval(pollTimer);
        pollTimer = null;
        document.removeEventListener("scroll", onScroll, true);
        window.removeEventListener("resize", onScroll);
        document.removeEventListener("keydown", onKeydown);
        removeLayers();
        restoreState();
        // Любое закрытие (Готово/Скрыть/Escape) — «seen»; перезапуск из
        // кнопок шлёт mark:false.
        if (!(opts && opts.mark === false)) markSeen();
        steps = [];
        index = -1;
        cfg = null;
    }

    function isActive() {
        return active;
    }

    // === Конфиг и повторный показ ===

    function readConfig() {
        var node = document.getElementById(CONFIG_ID);
        if (!node) return null;
        try {
            var parsed = JSON.parse(node.textContent || "null");
            return parsed && typeof parsed === "object" ? parsed : null;
        } catch (e) {
            return null; // битый конфиг — живём без тура
        }
    }

    // Конфига нет вовсе, но кнопка повторного показа есть: scope на теге body.
    function fallbackConfig() {
        var scope = document.body && document.body.dataset
            ? String(document.body.dataset.aiWizardScope || "")
            : "";
        if (!scope) return null;
        return { scope: scope, version: 1, show: true, role: "", mark_url: "" };
    }

    function restartTour(conf, scopeOverride) {
        if (bootTimer) {
            clearTimeout(bootTimer);
            bootTimer = null;
        }
        var scope = String(scopeOverride || conf.scope || "");
        if (!scope) return;
        if (active) stop({ mark: false }); // перезапуск отметку не шлёт
        start(scope, conf);
    }

    function startFromDocsBtn() {
        var conf = readConfig() || fallbackConfig();
        if (!conf || !conf.scope) return;
        // Тур поверх модалки документации не нужен: закрываем штатным крестиком.
        var closeBtn = document.getElementById("aiDocsClose");
        if (closeBtn) {
            try {
                closeBtn.click();
            } catch (e) {}
        }
        // Один кадр — скрытие модалки ложится в layout, затем показываем тур.
        requestAnimationFrame(function () {
            restartTour(conf, null);
        });
    }

    function startFromRestartBtn(btn) {
        var conf = readConfig() || fallbackConfig();
        var scope = btn.dataset && btn.dataset.scope
            ? String(btn.dataset.scope)
            : (conf ? String(conf.scope || "") : "");
        if (!conf || !scope) return;
        restartTour(conf, scope);
    }

    // Один делегированный слушатель — всегда, даже при show:false / конфига
    // нет вовсе: кнопки повторного показа обязаны работать.
    function onDelegatedClick(event) {
        var target = event.target;
        if (!target || typeof target.closest !== "function") return;
        if (target.closest("#aiDocsTourBtn")) {
            startFromDocsBtn();
            return;
        }
        var restartBtn = target.closest("[data-ai-wizard-restart]");
        if (restartBtn) startFromRestartBtn(restartBtn);
    }

    // === Boot ===

    // DOMContentLoaded (НЕ window.onload: пользовательские страницы сами его
    // выставляют и затёрли бы наш обработчик).
    function boot() {
        document.addEventListener("click", onDelegatedClick);
        var conf = readConfig();
        if (!conf || conf.show !== true) return;
        // Небольшая пауза: восстанавление персистенса/инициализация страницы
        // успевают успокоиться, затем раскладка не прыгает.
        bootTimer = setTimeout(function () {
            bootTimer = null;
            if (active) return;
            start(conf.scope, conf);
        }, START_DELAY_MS);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    window.AIWizard = { start: start, stop: stop, isActive: isActive };
})();