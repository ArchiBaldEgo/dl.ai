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
 *
 * v2: автостарт ждёт window.load; отметка «seen» — только по жесту
 * пользователя (автозакрытие цели флага не сжигает); подсветка — пересечение
 * цели с вьюпортом (без «сжатия до 60%»); общие шаги solve/find_error (common)
 * пропускаются, когда сервер сказал basics_seen; шаги чата common не имеют —
 * чат-тур всегда полный (строгий порядок — ранги); общий тур админки — три
 * ролевых scope'а (admin_pd/admin_staff/admin_super).
 *
 * v3: тур обязан идти СВЕРХУ ВНИЗ — порядок шагов движок сортирует по
 * вертикальной позиции цели (явный rank сильнее авторазмерки: хром админки);
 * цель, которую открыл хук (левое меню, меню «Процессы»), ДОЖИДАЕТСЯ
 * ограниченным ожиданием вместо мгновенного пропуска — переходы CSS
 * анимируются, и мгновенный замер пропускал ВСЕ шаги меню админки; повторные
 * замеры серии после первого кадра; у главных страниц админки — свои туры
 * (только контент страницы, хром расписывает главный экран).
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
    var RECHECK_MS = 400;    // интервал пере-замера (живой DOM чата/меню)
    // Повторные замеры шага после первого кадра: открытие левого меню и меню
    // «Процессы» анимируется переходами, media-запросы по zoom применяются
    // асинхронно — первый замер может поймать цель ещё невидимой или за
    // кадром. Раскладка доезжает — пятно само появится.
    var REMEASURE_MS = 150;
    var REMEASURE_LATER = [REMEASURE_MS, RECHECK_MS, 750];
    var MISSED_TICKS = 3;    // тиков без видимой цели подряд — тур сдаётся
    var WAIT_TICK_MS = 120;  // шаг ожидания цели после хука (openNav и др.)
    var WAIT_MAX_TICKS = 14; // ≈1.7 с: дольше — цель не появится, шаг пропускаем
    var READY_DELAY_MS = 300;  // пауза ПОСЛЕ window.load перед автостартом
    var MAX_BOOT_WAIT_MS = 3000; // load не пришёл (завис ресурс) — стартуем без него
    // «Лист» — нижняя карточка на тесном экране; порог совпадает с breakpoint'ом
    // перестроения панелей чата (ai.css: stack ≤ 768), иначе в полосе 641–768
    // карточки валятся на вертикальный стек.
    var SHEET_MAX = 768;
    var SHEET_QUERY = "(max-width: 768px)";

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

    // === Локализация юзер-туров (правило триязычной клиентской части) ===
    // Только scope'ы chat/solve/find_error; админские туры остаются русскими.
    // Русские тексты живут в REGISTRY (источник истины), таблица несёт en/fr;
    // язык читается на старте тура из #selectLang (кука ai_state — фолбэк).
    var LABELS_TR = {
        ru: { prev: LABEL_PREV, next: LABEL_NEXT, done: LABEL_DONE, skip: LABEL_SKIP, progress: " из " },
        en: { prev: "Back", next: "Next", done: "Done", skip: "Hide", progress: " of " },
        fr: { prev: "Retour", next: "Suivant", done: "Prêt", skip: "Masquer", progress: " sur " }
    };

    var USER_STEP_TR = {
        en: {
            chat: {
                "#selectLang": { title: "Page language", text: "This changes the language of the interface: Russian, English, French." },
                "#select": { title: "Model selection", text: "These are different AI helper models. Click and pick who will answer you." },
                "#thinkToggleBtn": { title: "The Think button", text: "Visible only for Ollama models. Off — the model answers right away; on — you can first look at its reasoning (the “Thinking” block above the answer)." },
                "#selectModelSort": { title: "Model sorting", text: "You can show faster or more precise models first." },
                "#selectType": { title: "Mode", text: "Modes: chat, “Solve the task”, “What's the error?”. Switch through the list." },
                "#themeToggleBtn": { title: "Light or dark", text: "Click — the page turns dark. Click again — light." },
                "#userDocsBtn": { title: "The “?” button", text: "This is the user guide. Click if anything is unclear. You can also show these tips again there." },
                "#voiceModeBtn": { title: "Voice mode", text: "Would you rather speak than type? Click it — you can speak by voice." },
                "#content .toggle-button": { title: "Side menu", text: "The arrow on the right opens the menu with a link to the admin panel." },
                "#messages": { title: "History", text: "History: everything you write and the model answers appears here." },
                "#messageText": { title: "Type your question here", text: "Just type your question into this field." },
                '.buttons-block button[type="submit"]': { title: "The “Send” button", text: "When the question is written, click this button." },
                '[onclick="clearContext()"]': { title: "Erase everything", text: "This button clears the conversation and starts over." }
            },
            solve: {
                "#selectType": { title: "Where you are", text: "You are on the “Solve the task” page. Use this list to return to “Chat”." },
                "#selectLang": { title: "Page language", text: "This changes the interface language." },
                "#select": { title: "Model selection", text: "The list of model helpers that will solve the task." },
                "#selectProgLng": { title: "Programming language", text: "First choose the language of your code: C or Assembler." },
                "#selectTheme": { title: "Topic", text: "Pick a topic — it tells the model what the task is about." },
                "#selectPrompt": { title: "Preprompt", text: "A preprompt is a small hint for the model. The selected one is fine." }
            },
            find_error: {
                "#selectType": { title: "Where you are", text: "You are on the “What’s the error?” page. Use the list to return to “Chat” or “Solve the task”." },
                "#selectLang": { title: "Page language", text: "This changes the interface language." },
                "#select": { title: "Model selection", text: "Pick the model helper that will check your code." },
                "#taskText": { title: "Task text", text: "Copy the task condition from dl.gsu.by here." },
                "#codeText": { title: "Code text", text: "And paste the whole program here." }
            }
        },
        fr: {
            chat: {
                "#selectLang": { title: "Langue de la page", text: "C’est ici que change la langue des textes : Russe, English, Français." },
                "#select": { title: "Choix du modèle", text: "Voici différents modèles-assistants. Clique et choisis celui qui te répondra." },
                "#thinkToggleBtn": { title: "Le bouton Think", text: "Visible seulement pour les modèles Ollama. Désactivé — le modèle répond tout de suite ; activé — tu peux d’abord voir son raisonnement (le bloc « Réflexions » au-dessus de la réponse)." },
                "#selectModelSort": { title: "Tri des modèles", text: "Tu peux afficher en premier les modèles plus rapides ou plus précis." },
                "#selectType": { title: "Mode", text: "Modes : chat, « Résoudre la tâche », « Quelle est l’erreur ? ». On change via la liste." },
                "#themeToggleBtn": { title: "Clair ou sombre", text: "Clique — la page devient sombre. Clique encore — claire à nouveau." },
                "#userDocsBtn": { title: "Le bouton « ? »", text: "C’est le guide utilisateur. Clique si quelque chose n’est pas clair. Tu peux aussi réafficher ces astuces ici." },
                "#voiceModeBtn": { title: "Mode vocal", text: "Tu préfères parler plutôt que taper ? Clique — tu peux parler avec la voix." },
                "#content .toggle-button": { title: "Menu latéral", text: "La flèche à droite ouvre le menu, qui contient le lien vers le panneau admin." },
                "#messages": { title: "Historique", text: "Historique : tout ce que tu écris et que le modèle répond s’affiche ici." },
                "#messageText": { title: "Tu écris ta question ici", text: "Écris simplement ta question dans ce champ." },
                '.buttons-block button[type="submit"]': { title: "Le bouton « Envoyer »", text: "Quand la question est écrite, clique sur ce bouton." },
                '[onclick="clearContext()"]': { title: "Tout effacer", text: "Ce bouton efface la conversation et recommence." }
            },
            solve: {
                "#selectType": { title: "Où tu es", text: "Tu es sur la page « Résoudre la tâche ». Par cette liste, tu reviens au « Chat »." },
                "#selectLang": { title: "Langue de la page", text: "C’est ici que change la langue des textes." },
                "#select": { title: "Choix du modèle", text: "La liste des modèles-assistants qui résoudront la tâche." },
                "#selectProgLng": { title: "Langage de programmation", text: "Choisis d’abord le langage de ton code : C ou Assembleur." },
                "#selectTheme": { title: "Thème", text: "Choisis un thème — il explique au modèle de quoi parle la tâche." },
                "#selectPrompt": { title: "Pré-promp", text: "Le pré-prompt est une petite indication pour le modèle. Celle déjà choisie suffit." }
            },
            find_error: {
                "#selectType": { title: "Où tu es", text: "Tu es sur la page « Quelle est l’erreur ? ». Par la liste, reviens au « Chat » ou à « Résoudre la tâche »." },
                "#selectLang": { title: "Langue de la page", text: "C’est ici que change la langue des textes." },
                "#select": { title: "Choix du modèle", text: "Choisis le modèle-assistant qui vérifiera ton code." },
                "#taskText": { title: "Texte de la tâche", text: "Colle ici l’énoncé de la tâche depuis dl.gsu.by." },
                "#codeText": { title: "Texte du code", text: "Et colle ici le programme en entier." }
            }
        }
    };

    // Язык UI («Русский»/«English»/«Français»): текущий селект, иначе кука ai_state.
    function wizardUiLanguage() {
        var sel = document.getElementById("selectLang");
        if (sel && sel.value) return sel.value;
        try {
            var m = (document.cookie || "").match(/(?:^|;\s*)ai_state=([^;]*)/);
            if (m) {
                var state = JSON.parse(decodeURIComponent(m[1]));
                if (state && state.lang) return String(state.lang);
            }
        } catch (e) { /* нет куки или кривой JSON — дефолт (русский) */ }
        return "";
    }

    function uiSuffix(language) {
        var low = String(language || "").toLowerCase();
        if (low.indexOf("en") === 0) return "en";
        if (low.indexOf("fr") === 0) return "fr";
        return "ru";
    }

    // Локализация юзер-шагов по селектору цели (уникален в своём scope).
    function localizeUserSteps(scopeName, list) {
        var suffix = uiSuffix(wizardUiLanguage());
        var table = (USER_STEP_TR[suffix] || {})[scopeName];
        if (!table) return list;
        return list.map(function (st) {
            var tr = table[st.sel];
            if (!tr) return st;
            return Object.assign({}, st, { title: tr.title, text: tr.text });
        });
    }

    function currentLabels() {
        return LABELS_TR[cfg && cfg.lang ? cfg.lang : "ru"];
    }

    // === Реестр шагов (тексты — константы языка: правятся только здесь) ===
    // v3: маршрут строит движок — sortResolved ставит шаги сверху вниз по
    // позиции цели на экране. Списки ниже — полный НАБОР целей страницы,
    // а не маршрут. Явный rank (только у хрома админки) сильнее авторазмерки:
    // цели хрома скрыты на старте (меню «Процессы») или их вертикальная
    // позиция зависит от состояния левого меню.

    // Топ-бар («весь бар») и левое меню — хром главного экрана админки.
    // Порядок рангов = эталон пользователя: приветствие (внутри ФИО + права +
    // счётчик «Процессы») → открытые процессы → «Просмотр сайта» → поиск по
    // меню → левый бар; guest/выход — дополнительные шаги.
    var ADMIN_BAR = [
        { sel: "#aiProcessesToggle", title: "Приветствие", rank: 10, pos: "bottom", text: "Это приветствие. Внутри — твоё ФИО, бейдж уровня прав и счётчик «Процессы». Клик по имени открывает меню." },
        { sel: "#aiProcessesMenu", title: "Открытые процессы", rank: 11, pos: "left", onShow: "openProcesses", text: "Меню «Процессы»: свои запуски — что идёт сейчас и чем закончилось." },
        { sel: '#user-tools a[href="/ai/chat/"]', title: "Просмотр сайта", rank: 12, pos: "bottom", text: "Ссылка ведёт на пользовательскую часть: чат на /ai/chat/." },
        { sel: "#aiGuestToggle", title: "Посмотреть как другой", rank: 13, pos: "bottom", text: "Кнопка суперадмина: админка глазами разработчика промптов. Повторное нажатие вернёт всё назад." },
        { sel: "#logout-form button", title: "Выход", rank: 14, pos: "bottom", text: "Выход из админки. Рядом смена пароля, если она доступна." },
        { sel: "#nav-filter", title: "Поиск по меню", rank: 20, pos: "right", onShow: "openNav", text: "Поиск: разделов много? Набери пару букв — останется нужное." },
        { sel: "#toggle-nav-sidebar", title: "Край страницы", rank: 21, pos: "right", text: "Тонкая кнопка у левого края: открывает и закрывает левый бар с разделами." },
        { sel: "#nav-sidebar", title: "Левый бар", rank: 22, pos: "right", onShow: "openNav", text: "Левый бар: все разделы. Дальше — по каждому." }
    ];

    // Разработчик промптов (есть у всех): документация, свой промпт, пакетное
    // решение, журнал, закреплённые.
    var ADMIN_PD_EXTRA = [
        { sel: '#nav-sidebar a[href="/ai/admin/docs/prompt-developer/"]', title: "Документация", text: "Инструкция для тебя как разработчика промптов.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/prompts/my/"]', title: "Мой промпт", text: "Здесь ты пишешь и меняешь свои промпты.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/arm/solve/"]', title: "Пакетное решение", text: "Проверка моделей сразу на всех задачах курса.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/ai/request_logs/"]', title: "Журнал запросов", text: "Видно твои запросы к моделям.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/pinned-runs/"]', title: "Закреплённые", text: "Закреплённые пакетные решения — быстрый доступ к прогонам.", pos: "right", optional: true, onShow: "openNav" }
    ];

    // Дополнительно staff-админам: настройки приложения, модели, обновления.
    var ADMIN_STAFF_EXTRA = [
        { sel: '#nav-sidebar a[href="/ai/admin/ai/aiappsettings/"]', title: "Настройка ИИ-приложения", text: "Главные настройки. Только для админов.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/arm/models/"]', title: "Состояние моделей", text: "Какие модели работают, а какие нет.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href="/ai/admin/updates/"]', title: "Обновления", text: "Что нового появилось в проекте.", pos: "right", onShow: "openNav" }
    ];

    // Дополнительно суперадминам: дефолтные промпты, регрессия, тесты,
    // режим «посмотреть как другой», роль в шапке.
     var ADMIN_NAV_PAGES = [
        { sel: '#nav-sidebar a[href^="/ai/admin/ai/aiappsettings/"]', title: "Настройка ИИ-приложения", text: "Главные настройки: доступ к ИИ, сводка за сутки, последние прогоны. Свой тур — на самой странице.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/pinned-runs/"]', title: "Закреплённые прогоны", text: "Закреплённые (★) завершённые прогоны — быстрый доступ.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/prompts/my/"]', title: "Мой промпт", text: "Твои промпты: список, поиск, кнопка «Добавить».", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/prompt-defaults/"]', title: "Промпты по умолчанию", text: "Какие промпты подставляются автоматически по языку и теме.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/docs/prompt-developer/"]', title: "Инструкция промпт-разработчика", text: "Как писать промпты — читать перед работой.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/docs/developer/"]', title: "Документация разработчика", text: "Техническая документация проекта.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/docs/superuser/"]', title: "Инструкция суперадмина", text: "Что меняет только суперадмин.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/arm/solve/"]', title: "Пакетное решение", text: "Прогон моделей по всем задачам курса.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/arm/models/"]', title: "Состояние моделей", text: "Какие модели доступны и почему нет.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/ai/request_logs/"]', title: "Журнал запросов", text: "Все запросы к моделям: кто, что и чем закончилось.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/test-console/"]', title: "Тестовая консоль", text: "Запуск тестов приложения и логи прошлых запусков.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/test_data/"]', title: "Тестовые данные", text: "Генератор образцов: создаёт тестовые записи журнала и дневного отчёта — можно проверить, как выглядят страницы, не дожидаясь реальных запросов.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/ai/tasksolution/"]', title: "Решённые задачи", text: "Кэш: задания, прошедшие тест в DL, достаются без модели.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/updates/"]', title: "Обновления", text: "Что нового появилось в проекте.", pos: "right", onShow: "openNav" },
        { sel: '#nav-sidebar a[href^="/ai/admin/ai/prompt/"]', title: "Промпты", text: "Таблица всех промптов; правка — по клику на название.", pos: "right", onShow: "openNav" }
    ];


    var REGISTRY = {

        chat: [
            // Строгий порядок чата (ранг: явный rank сильнее геометрии):
            // язык → модель → think → сортировка → режим → тема → «?» →
            // голосовой → боковое меню (супер/штаб) → история → ввод →
            // отправить → очистить.
            // Шагов с common в чате НЕТ: basics_seen не режет чат — тур всегда
            // полный и строгий (ранги).
            { sel: "#selectLang", rank: 10, title: "Язык страницы", text: "Тут меняется язык надписей: Русский, English, Français. Сам текст вопросов и ответов — всегда на том языке, на котором ты пишешь.", pos: "bottom" },
            { sel: "#select", rank: 11, title: "Выбор модели", text: "Это разные помощники-модели: нажми и выбери, кто будет отвечать. Не знаешь, кого взять — оставь как есть: наверху стоит обычно та, что выбиралась чаще всего.", pos: "bottom" },
            { sel: "#thinkToggleBtn", rank: 12, skipIfHidden: true, title: "Кнопка Think", text: "Видна только у моделей Ollama. Выключено — модель отвечает сразу; включено — сначала можно посмотреть её рассуждения (блок «Размышления» над ответом).", pos: "bottom" },
            { sel: "#selectModelSort", rank: 13, title: "Сортировка моделей", text: "Здесь можно переложить список: сначала более быстрые модели или сначала более точные. Вернуть как было — режим «по алфавиту».", pos: "bottom" },
            { sel: "#selectType", rank: 14, title: "Режим", text: "Режимы: чат, «Реши задачу», «В чём ошибка». Через этот список переключаешься между страницами — всё твоё (модель, тема) запомнится.", pos: "bottom" },
            { sel: "#themeToggleBtn", rank: 15, title: "Светлое или тёмное", text: "Нажми — страница станет тёмной. Ещё раз — снова светлой. Как тебе удобнее читать.", pos: "bottom" },
            { sel: "#userDocsBtn", rank: 16, title: "Кнопка «?»", text: "Это полная инструкция — нажми, если что-то непонятно. Она есть на русском, английском и французском (язык как у страницы); если перевод не подгрузился — покажется русская версия. Там же можно снова показать эти подсказки.", pos: "bottom" },
            { sel: "#voiceModeBtn", rank: 17, title: "Голосовой ввод", text: "Хочешь говорить, а не печатать? Нажми — можно говорить голосом.", pos: "bottom" },
            { sel: "#content .toggle-button", rank: 18, title: "Боковое меню", text: "Стрелочка справа открывает меню, там ссылка на админ-панель.", pos: "left", roles: ["super", "staff"] },
            { sel: "#messages", rank: 19, title: "История", text: "История: всё, что ты пишешь и что отвечает модель, видно здесь.", pos: "center" },
            { sel: "#messageText", rank: 20, title: "Здесь пишешь вопрос", text: "Просто печатай свой вопрос в это поле.", pos: "top" },
            { sel: '.buttons-block button[type="submit"]', rank: 21, title: "Кнопка «Отправить»", text: "Написал вопрос — жми эту кнопку.", pos: "top" },
            { sel: '[onclick="clearContext()"]', rank: 22, title: "Стереть всё", text: "Эта кнопка стирает переписку и начинает сначала.", pos: "top" }
        ],

        // Эталон пользователя (v4): «Реши задачу — Язык программирования, тема,
        // промпт». Общие шаги (язык/модель/режим) добавляются сами, когда базу
        // ещё не показывали; всё остальное — только на чате.
        solve: [
            { sel: "#selectType", title: "Где ты", text: "Ты на странице «Реши задачу». Через этот список вернёшься в «Чат».", pos: "bottom", common: true },
            { sel: "#selectLang", title: "Язык страницы", text: "Тут меняется язык надписей.", pos: "bottom", common: true },
            { sel: "#select", title: "Выбор модели", text: "Список моделей — помощников, которые будут решать задачу. Рядом с именем могут стоять цифры: % решённых задач и среднее время ответа. Смотри на них как на подсказку: они показывают, как модель справляется именно с твоим выбором ниже.", pos: "bottom", common: true },
            { sel: "#selectProgLng", title: "Язык программирования", text: "Выбери, на каком языке пишешь код: C или Ассемблер. От выбора зависит всё остальное: какие будут темы, промпты и цифры у моделей выше.", pos: "bottom" },
            { sel: "#selectTheme", title: "Тема", text: "Выбери тему — она объясняет модели, о чём задача. Выбрал тему — цифры у моделей пересчитаются под статистику именно этой темы.", pos: "top" },
            { sel: "#selectPrompt", title: "Промпт", text: "Промпт — маленькая подсказка для модели: как ей отвечать и оформлять код. Обычно подходит тот, что уже выбран.", pos: "top" }
        ],

        // Эталон пользователя (v4): «В чём ошибка — текст для задачи, текст для
        // кода». Селекты языка/темы/промпта объясняет тур «Реши задачу» —
        // здесь их не повторяем.
        find_error: [
            { sel: "#selectType", title: "Где ты", text: "Ты на странице «В чём ошибка». Через список вернёшься в «Чат» или в «Реши задачу».", pos: "bottom", common: true },
            { sel: "#selectLang", title: "Язык страницы", text: "Тут меняется язык надписи.", pos: "bottom", common: true },
            { sel: "#select", title: "Выбор модели", text: "Выбери помощника-модель, которая проверит код. Рядом с именем могут стоять цифры: % решённых задач и среднее время — считаются по выбранному языку и теме (нет цифр — по такому сочетанию модель ещё не прогонялась).", pos: "bottom", common: true },
            { sel: "#taskText", title: "Текст для задачи", text: "Вставь сюда условие задачи целиком — скопируй его со страницы dl.gsu.by, где задача написана.", pos: "top" },
            { sel: "#codeText", title: "Текст для кода", text: "А ниже вставь свою программу целиком — модель посмотрит её вместе с условием и скажет, где ошибка.", pos: "top" }
        ],

        // Главный экран админки — свой scope на каждый уровень прав (флаг
        // независим), шаги ОДИНАКОВЫ: пункты меню, скрытые правами этого
        // уровня, отсутствуют в DOM и выпадают сами (v1-принцип: роль режется
        // DOM, без ветвлений). Сборка concat'ом, а не копия списка (DRY).
        admin_pd: ADMIN_BAR.concat(ADMIN_NAV_PAGES),

        admin_staff: ADMIN_BAR.concat(ADMIN_NAV_PAGES),

        admin_super: ADMIN_BAR.concat(ADMIN_NAV_PAGES),

        admin_arm_solve: [
            { sel: "#armCourseId", title: "Номер курса", text: "Это номер курса на dl.gsu.by. Он уже вписан — не меняй без нужды.", pos: "right" },
            { sel: "#armLoadTreeBtn", title: "Загрузить задачи", text: "Нажми — подтянутся задачи курса из dl.gsu.by.", pos: "right" },
            { sel: "#armTreeContainer", title: "Дерево задач", text: "Отметь задачи, на которых проверить модели.", pos: "right" },
            { sel: "#armTreeSelectAll", title: "Выбрать всё", text: "Отметит все задачи разом. Появляется после загрузки.", pos: "right" },
            { sel: "#armSelectAllModels", title: "Модели", text: "Отметь модели, которые будут решать. Ничего не отмечено — значит все.", pos: "bottom" },
            { sel: "#armLanguageChecks", title: "Языки", text: "Выбери один или несколько языков: на каждый запустится свой процесс.", pos: "bottom" },
            { sel: "#armExtensionInput", title: "Расширение", text: "Расширение файла определяется само, из выбранных языков.", pos: "right" },
            { sel: "#armTopicSelect", title: "Тема", text: "Станет доступной после выбора языка.", pos: "right" },
            { sel: "#armPromptSelect", title: "Промпт", text: "Обычно «По привязке» — подставится промпт по умолчанию. Можно выбрать свой.", pos: "right" },
            { sel: "#armSaveSolutions", title: "Кэш решений", text: "Если задача прошла тест в DL, её решение сохранится — потом достанется без вызова модели.", pos: "right" },
            { sel: "#armRecordStats", title: "Статистика", text: "Суперадмин может заносить результаты в базу — они попадут в селектор моделей чата.", pos: "right" },
            { sel: "#armRunName", title: "Название прогона", text: "Придумай короткое имя — так прогон потом легко найти в журнале.", pos: "right" },
            { sel: ".arm-warning", title: "Важно", text: "Пока идёт прогон, лучше не уходить со страницы — тестирование прервётся.", pos: "bottom" },
            { sel: "#armSolveSubmit", title: "Запустить", text: "Всё выбрал — жми эту кнопку. Прогон появится в «Процессах» в шапке.", pos: "top" },
            { sel: "#armSolveProcessesCard", title: "Процессы", text: "Здесь список запущенных прогонов. Оттуда он открывается в таблицу результатов.", pos: "top" }
        ],

        admin_model_status: [
            { sel: "#statusHealthWindowDate", title: "Окно проверки", text: "Модели проверяются раз в сутки в 04:00. Дата последнего окна — здесь.", pos: "bottom" },
            { sel: "#refreshModelsBtn", title: "Обновить сейчас", text: "Не хочешь ждать ночи — нажми, и модели проверятся сразу.", pos: "bottom" },
            { sel: "#refreshInProgressHint", title: "Идёт проверка", text: "Пока работает проверка, страница обновляется сама.", pos: "right" },
            { sel: "#modelStatusRowsBody", title: "Список моделей", text: "«Активна» — работает; «Неактивна» — надо разбираться. В таблице время, код и расшифровка.", pos: "top" }
        ],

        // === Туры главных страниц админки: только КОНТЕНТ страницы — бар и
        // меню расписывает главный экран, чтобы не повторяться. Сверху вниз
        // ставит движок; цель, которой нет в DOM, просто выпадает.

        // Журнал запросов (list + detail: resend-btn только там) + дневной
        // отчёт (путь /daily-report/ того же scope: на нём шаги журнала
        // выпадают сами — целей нет в DOM, и наоборот). Кнопок «Фильтровать»/
        // «Обновить таблицу» больше нет: любой фильтр применяется сам.
        admin_logs: [
            { sel: "#id_mode", title: "Режим запроса", text: "Выбираешь, какие записи показывать: Чат, «Реши задачу», «В чём ошибка» или пакетное решение. Выбрал — таблица обновится СРАЗУ сама, без кнопок. Верни «Все» — снова будут видны все записи.", pos: "bottom" },
            { sel: "#id_model", title: "Модель", text: "Ищи записи по модели: выбери её из списка — останутся только записи, где отвечала она. Работает сразу, как только выбрал.", pos: "bottom" },
            { sel: "#id_user", title: "ФИО", text: "Начни печатать фамилию (например, «Ива») — поиск найдёт человека по ФИО, ID или логину. Срабатывает само, через мгновение после набора: кнопки нажимать не нужно.", pos: "bottom" },
            { sel: "#id_date_from", title: "С какой даты", text: "Показать записи с этой даты включительно. Дата выбрана — таблица обновилась сама.", pos: "bottom" },
            { sel: "#id_date_to", title: "По какую дату", text: "И до какой даты (включая её). Можно заполнить только одну из двух границ — тогда период с одной стороны открыт.", pos: "bottom" },
            { sel: ".ai-logs-actions", title: "Кнопки под фильтрами", text: "«Дневной отчёт» — сводка за сегодня: кто из студентов сколько и о чём спрашивал. «Сбросить» — очистить все выбранные фильтры и показать журнал целиком.", pos: "bottom" },
            { sel: ".ai-logs-table", title: "Таблица запросов", text: "Клик по строке раскроет подробный отчёт: что спрашивали, что ответила модель, к какой задаче это относится. Ничего не нашлось — чаще всего это фильтры, а не отсутствие записей: жми «Сбросить».", pos: "bottom" },
            { sel: "#resend-btn", title: "Повторить", text: "Отправит модели тот же вопрос ещё раз — удобно сравнить свежий ответ со старым. Кнопка есть в раскрытом отчёте запроса.", pos: "bottom" },
            // Шаги дневного отчёта: live только на его странице — из тура
            // журнала выпадают (целей нет), как и шаги журнала на отчёте.
            { sel: "#daily-filter-student", title: "Поиск по ФИО", text: "Начни печатать фамилию — таблица сузится сама, через мгновение после набора. Никаких кнопок нажимать не нужно.", pos: "bottom" },
            { sel: "#daily-filter-group", title: "Фильтр «Группа»", text: "Выбери группу — в таблице останутся только её студенты. Список групп собирается из записей за сегодня, пустых групп в нём не будет. Выбор применяется сам, сразу.", pos: "bottom" },
            { sel: "#daily-filter-school", title: "Фильтр «Вуз/Школа»", text: "То же самое, но по вузу/школе. Можно включить оба фильтра сразу — например, одна группа одного вуза. Выбор применяется сам, сразу — кнопки нет.", pos: "bottom" },
            { sel: ".ai-daily-table", title: "Кто и сколько спрашивал", text: "Одна строка — один студент за сегодня: ФИО, группа и вуз/школа, когда спрашивал последний раз, последняя тема и промпт и сколько всего запросов. Клик по строке раскроет все его запросы за день, каждая запись — кликом ещё раз.", pos: "bottom" }
        ],

        admin_pinned_runs: [
            { sel: ".ai-pinned-table", title: "Закреплённые прогоны", text: "Клик по строке — результаты прогона откроются прямо здесь.", pos: "bottom" },
            { sel: ".ai-log-pin-btn", title: "Снять закрепление", text: "★ убирает прогон из закреплённых. Поставить ★ — в журнале запросов.", pos: "bottom" }
        ],

        admin_tasksolution: [
            { sel: "#ai-sol-table", title: "Решённые задачи", text: "Кто, какую задачу и какой моделью решил. Клик по строке (не по ссылке) — покажет сохранённый код.", pos: "bottom" }
        ],

        admin_aiappsettings: [
            { sel: "#id_is_enabled", title: "Главный выключатель", text: "Выкл — доступ к ИИ закрыт у всех сразу. Вкл — работать можно. Не забудь после изменения нажать «Сохранить» внизу формы.", pos: "right" },
            { sel: ".ai-daily-report-bar", title: "Сводка за сутки", text: "Сколько было запросов и решённых задач за последние сутки — на глаз видно, «живой» ли сервис.", pos: "right" },
            { sel: "table.ai-recent-logs-table:not(.ai-batch-table)", title: "Последние запросы", text: "Одна строка — один запрос: кто спрашивал, время отправки и получения, из какого режима, с какого языка/темы и промпта, какая модель и чем закончилось. Клик по строке раскроет полный запрос и ответ целиком.", pos: "bottom" },
            { sel: "table.ai-batch-table", title: "Последние пакетные решения", text: "Клик по строке — результаты прогона (раскроются под ним). Колонка «Время» показывает, сколько шёл прогон от запуска до окончания — удобно сравнить прогоны между собой.", pos: "bottom" },
            { sel: ".submit-row input[name=_save]", title: "Сохранить", text: "После изменения настроек не забудь нажать.", pos: "right" }
        ],

        admin_updates: [
            { sel: '.ai-updates-filter input[name="q"]', title: "Поиск", text: "Ищет по описанию и автору коммита. Печатай — список сузится сам, кнопки нет.", pos: "bottom" },
            { sel: '.ai-updates-filter select[name="author"]', title: "Автор", text: "Записи одного человека; применяется сразу, как только выбрал. Поправить имя автора — в справочнике AuthorAlias.", pos: "bottom" },
            { sel: '.ai-updates-filter input[name="date_from"]', title: "Период", text: "С какой даты показывать; применяется само, как только выбрал дату.", pos: "bottom" },
            { sel: "#content-main table", title: "История изменений", text: "Дата, автор, что сделано — новые сверху.", pos: "bottom" },
            { sel: 'a[href*="updatelog/"]', title: "Скрытые записи", text: "Коммиты, ещё не показанные пользователям в этом списке.", pos: "bottom" }
        ],

        admin_regression: [
            { sel: "#prt_prompt", title: "Промпт", text: "Какой промпт проверяем.", pos: "right" },
            { sel: "#prt_interface_language", title: "Язык страницы", text: "На каком языке отвечают пользователи — тот и проверяем.", pos: "right" },
            { sel: "#prt_models", title: "Модели", text: "Какими моделями прогнать тест.", pos: "right" },
            { sel: "#prt_cases", title: "Тест-кейсы", text: "Контрольные примеры: вопрос и правильный ответ.", pos: "right" },
            { sel: "#prtRunSubmitBtn", title: "Запустить", text: "Старт проверки; ход будет виден под формой.", pos: "right" },
            { sel: "#prtReportCard", title: "Сводка", text: "Сколько совпало и сколько промахов.", pos: "top" },
            { sel: "#prtResultsCard", title: "Подробности", text: "Разбор случаев, где ответ разошёлся с эталоном.", pos: "top" }
        ],

        admin_test_console: [
            { sel: "#tcRunSubmitBtn", title: "Запустить проверки", text: "Полный прогон тестов приложения; ход — полоса рядом.", pos: "right" },
            { sel: "#tcSimpleBanner", title: "Что происходит", text: "Короткий статус: что запущено и чем окончилось.", pos: "bottom" },
            { sel: "#tcSummary", title: "Итог", text: "Сколько тестов прошло и сколько упало.", pos: "top" },
            { sel: "#tcLog", title: "Лог прогона", text: "Полный вывод тестов.", pos: "top" },
            { sel: "#tcHistoryList", title: "История запусков", text: "Прошлые прогоны; клик — открыть их лог.", pos: "top" }
        ],

        admin_my_prompt: [
            { sel: "a.addlink", title: "Добавить промпт", text: "Откроется форма нового промпта: имя и текст на трёх языках, темы.", pos: "bottom" },
            { sel: '#changelist input[name="q"]', title: "Поиск", text: "Ищет по названию и тексту промпта.", pos: "bottom" },
            { sel: "#changelist-filter", title: "Фильтры", text: "Оставить только нужное: язык, тема, владелец.", pos: "bottom" },
            { sel: "#result_list", title: "Список промптов", text: "Твои промпты. Клик по названию — открыть и править.", pos: "top" }
        ],

        admin_prompt_defaults: [
            { sel: "#pdLanguage", title: "Язык", text: "Для какого языка программирования задаём привязку.", pos: "right" },
            { sel: "#pdTopic", title: "Тема", text: "Подтема курса; станет активной, когда выбран язык.", pos: "right" },
            { sel: "#pdMode", title: "Вид ARM", text: "Где подставлять: на «Реши задачу» или «В чём ошибка».", pos: "right" },
            { sel: "#pdPrompt", title: "Промпт", text: "Он подставится по умолчанию у всех пользователей.", pos: "right" },
            { sel: "#pdSaveBtn", title: "Сохранить", text: "Жми — привязка появится в списке ниже.", pos: "right" },
            { sel: "#content-main table", title: "Существующие привязки", text: "Всё, что задано: «Изменить» — поправить, «Удалить» — убрать.", pos: "top" }
        ],

        // Генератор тестовых записей журнала (только суперюзер): страница
        // одна-экранная — 4 шага.
        admin_test_data: [
            { sel: ".test-data-page h2", title: "Генератор тестовых записей", text: "Страница-помощник: она создаёт фальшивые записи в «Журнале запросов», чтобы было что смотреть и фильтровать. Настоящих запросов студентов она не трогает и ничего не ломает.", pos: "bottom" },
            { sel: ".help-test-data", title: "Что появится в журнале", text: "После нажатия кнопки в журнале появится случайная запись: случайный студент (или сам админ), случайный режим — чат, «Реши задачу» или «В чём ошибка» — и случайная модель. Нажимаешь кнопку несколько раз — записей станет несколько.", pos: "bottom" },
            { sel: "[data-emit-request] .button", title: "Кнопка «Emit Request»", text: "Одно нажатие = одна новая тестовая запись в журнале. Чтобы проверить фильтры журнала, наделай записей пять-десять и иди в «Журнал запросов» — они появятся там наверху.", pos: "bottom" },
            { sel: "#emit-status", title: "Статус", text: "Здесь коротко напишется, что запись создана (или что пошло не так). Тестовые записи выглядят в журнале как настоящие — если захочешь их убрать, удали в стоковом списке журнала (/ai/admin/ai/airequestlog/) или просто оставь: ничему не мешают.", pos: "bottom" }
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
    var bootCancelled = false; // ручной запуск отменяет отложенный автостарт
    var marked = false;        // отметку «seen» шлём один раз за активацию
    var interacted = false;    // был ли в этом туре жест пользователя
    var missedTicks = 0;       // тиков подряд без видимой цели текущего шага
    var pendingWait = false;   // show() ждёт появления цели после хука
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
        // Прод переименовывает куки-префиксы Django (CSRF_COOKIE_NAME=
        // ai_csrftoken, DEPLOY.md): точное имя «csrftoken» там не встречается,
        // и отметка «seen» уходила БЕЗ токена → 403 → флаг «просмотрено»
        // никогда не записывался. Читаем и штатное, и переименованное имя.
        var cookies = document.cookie.split(";");
        for (var i = 0; i < cookies.length; i++) {
            var parts_ = cookies[i].trim().split("=");
            var name = parts_[0].toLowerCase();
            if (name === "csrftoken" || name.slice(-"csrftoken".length) === "csrftoken") {
                return parts_[1];
            }
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
        parts.btnSkip.textContent = currentLabels().skip;
        parts.btnSkip.addEventListener("click", function () {
            stop({ mark: true });
        });

        parts.btnPrev = document.createElement("button");
        parts.btnPrev.type = "button";
        parts.btnPrev.className = BTN_PREV;
        parts.btnPrev.textContent = currentLabels().prev;
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

    // Порядок шагов: маршрут строится движком, «сверху вниз» — требование.
    // Явный rank сильнее (хром админки: цели скрыты на старте или их позиция
    // зависит от состояния левого меню). Остальные — по вертикальной позиции
    // цели (гранула 8 px: соседи одной строки идут слева направо). Немеряемые
    // цели (display:none без rank) — в конец, в порядке реестра; Array.sort
    // стабилен (ES2019), при равных ключах реестр и есть тай-брейкер.
    var ORDER_UNMEASURED = 1e9;

    function stepOrderKey(step) {
        if (typeof step.rank === "number") return { tier: 0, rank: step.rank };
        var el = querySel(step.sel);
        if (!el) return { tier: 1, y: ORDER_UNMEASURED, x: 0 };
        var r = el.getBoundingClientRect();
        if (!r.width && !r.height && !r.left && !r.top) {
            return { tier: 1, y: ORDER_UNMEASURED, x: 0 };
        }
        return { tier: 1, y: Math.round(r.top / 8), x: r.left };
    }

    function sortResolved(out) {
        return out.slice().sort(function (a, b) {
            var ka = stepOrderKey(a);
            var kb = stepOrderKey(b);
            if (ka.tier !== kb.tier) return ka.tier - kb.tier;
            if (ka.tier === 0) return ka.rank - kb.rank;
            if (ka.y !== kb.y) return ka.y - kb.y;
            return ka.x - kb.x;
        });
    }

    // Фильтр реестра: (а) роль — шаг с roles[] участвует, только если список
    // пуст или содержит cfg.role (на пользовательских страницах role="",
    // поэтому участвуют только шаги без roles); (б) «общие» шаги (common:
    // язык/модель/переключатель solve/find_error) отбрасываются, когда сервер
    // отдал basics_seen — общая часть уже показана на другой странице, тур
    // должен ДОПОЛНЯТЬ прошлые, а не повторяться (в чате common нет — тур
    // чата всегда полный); (в) наличие цели: элемент
    // есть и виден. Для шагов с onShow видимость пока не требуем — хук сам
    // покажет цель (напр. #aiProcessesMenu скрыт до openProcesses), а ожидание
    // появления берёт на себя waitAndShow в show().
    function resolveSteps(scope, role, basicsSeen) {
        var list = REGISTRY[scope];
        var out = [];
        if (!list || !list.length) return out;
        for (var i = 0; i < list.length; i++) {
            var s = list[i];
            if (basicsSeen && s.common) continue;
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
        return sortResolved(out);
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

    function scrollToTarget(step) {
        // Скроллим ОБЕ оси: #selectPrompt и соседние селекты могут лежать в
        // горизонтально скроллируемой панели — с одной вертикалью цель
        // оставалась бы за обрезом и пятно не появилось бы.
        var el = querySel(step.sel);
        if (!el) return;
        try {
            el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        } catch (e) {
            try {
                el.scrollIntoView();
            } catch (e2) { /* без скролла — пятно всё равно рисуется */ }
        }
    }

    // Цель текущего шага реально во вьюпорте (пересечение непусто)?
    function targetInViewport(step) {
        var el = querySel(step.sel);
        if (!el || !isVisible(el)) return false;
        var root = document.documentElement;
        return Boolean(rectInViewport(
            el.getBoundingClientRect(),
            root ? root.clientWidth : window.innerWidth,
            root ? root.clientHeight : window.innerHeight,
        ));
    }

    // Хук (openNav/openProcesses) запускает CSS-переходы: «открыл и замерил»
    // нельзя — ширина левого меню в первый кадр ещё 0, и мгновенный пропуск
    // шага уронил ВСЕ шаги меню админки («в админке кучу всего» без зоны).
    // Ждём появления цели ограниченно: раскладка доедет — показываем.
    function waitAndShow(step, seqAtCall, n) {
        if (!active || runSeq !== seqAtCall) return;
        var el = querySel(step.sel);
        if (el && isVisible(el)) {
            pendingWait = false;
            scrollToTarget(step);
            requestAnimationFrame(function () {
                if (!active || runSeq !== seqAtCall) return;
                renderContent(step);
                place();
                // Серия повторов: переходы меню и media-запросы по zoom
                // доезжают асинхронно, первый кадр бывает со старой
                // геометрией. Повторы СНАЧАЛА проверяют, что цель во
                // вьюпорте, и скроллят только «за кадром» — без рывков.
                for (var i = 0; i < REMEASURE_LATER.length; i++) {
                    (function (ms) {
                        setTimeout(function () {
                            if (!active || runSeq !== seqAtCall) return;
                            if (!targetInViewport(step)) scrollToTarget(step);
                            place();
                        }, ms);
                    })(REMEASURE_LATER[i]);
                }
            });
            return;
        }
        if (n >= WAIT_MAX_TICKS) {
            pendingWait = false;
            skipForward();
            return;
        }
        setTimeout(function () {
            waitAndShow(step, seqAtCall, n + 1);
        }, WAIT_TICK_MS);
    }

    function show(i) {
        if (!active) return;
        if (i < 0 || i >= steps.length) {
            stop();
            return;
        }
        index = i;
        missedTicks = 0;
        var step = steps[i];
        // Шаг с skipIfHidden, чья цель ЕСТЬ, но скрыта сейчас (напр. кнопка
        // Think скрыта, пока не выбрана модель Ollama) — пропускаем мгновенно,
        // не съедая ограниченное ожидание: это не «цель ещё не доехала»
        // (тогда не помечали бы skipIfHidden), а контракт «скрыта сейчас =
        // шаг не для этого состояния». Отсутствие в DOM — обычный путь
        // waitAndShow → skipForward.
        if (step.skipIfHidden) {
            var el0 = querySel(step.sel);
            if (el0 && !isVisible(el0)) {
                show(i + 1);
                return;
            }
        }
        // Сначала хук (он может открыть навигацию/меню под целью — их
        // переходы анимированы), затем ограниченное ожидание появления,
        // скролл и замер в следующем кадре.
        if (step.onShow) runHook(step.onShow);
        var seqAtCall = runSeq;
        pendingWait = true;
        waitAndShow(step, seqAtCall, 0);
    }

    // Шаг сам исчез (перерисовка DOM): идём дальше, на последнем — финиш.
    // Автозакрытие без жеста отметку шлёт только когда пользователь в туре
    // уже участвовал (см. stop) — самопрогореть флаг не может.
    function skipForward() {
        if (index + 1 < steps.length) {
            show(index + 1);
        } else {
            stop();
        }
    }

    function next() {
        if (!active) return;
        interacted = true; // «Далее»/«Готово» — жест пользователя
        if (index + 1 < steps.length) {
            show(index + 1);
        } else {
            stop({ mark: true });
        }
    }

    function prev() {
        if (!active) return;
        interacted = true;
        if (index > 0) show(index - 1);
    }

    function renderContent(step) {
        parts.title.textContent = step.title || "";
        parts.text.textContent = step.text || "";
        parts.progress.textContent = (index + 1) + currentLabels().progress + steps.length;
        parts.btnPrev.disabled = index === 0;
        var last = index === steps.length - 1;
        parts.btnNext.textContent = last ? currentLabels().done : currentLabels().next;
        parts.btnNext.className = last ? BTN_DONE : BTN_NEXT;
        // Вхождение — заново на каждом шаге (subtle).
        popover.classList.remove(POP_ENTER);
        void popover.offsetWidth; // reflow, чтобы анимация переигралась
        popover.classList.add(POP_ENTER);
        popover.focus({ preventScroll: true });
    }

    // === Позиционирование ===

    // Подсветка — пересечение цели с вьюпортом. Раньше здесь были два
    // само-ограничения, гасившие пятно у реальных целей: MIN_SPOT=24
    // («мелкая цель не заслуживает пятна»: приветствие админки 14–21px,
    // селект промпта 19px) и SPOT_INSET=8, отъедавший цели, прижатые
    // к краям окна (стрелочка чата: right:0 → пересечение 16px). Для всех
    // таких шагов пятна не было, поповер уезжал «в центр» без якоря и
    // стрелки — в глазах пользователя «шаг не отображается». Теперь пятно
    // рисуется на любом непустом пересечении с вьюпортом; куда встанет
    // поповер, решает sideFits (VIEW_MARGIN), а не размер цели.
    function rectInViewport(rect, viewW, viewH) {
        var left = Math.max(0, rect.left);
        var top = Math.max(0, rect.top);
        var right = Math.min(viewW, rect.right);
        var bottom = Math.min(viewH, rect.bottom);
        if (right - left <= 0 || bottom - top <= 0) return null;
        return {
            left: left, top: top, right: right, bottom: bottom,
            width: right - left, height: bottom - top
        };
    }

    function sheetMode() {
        var viewW = document.documentElement ? document.documentElement.clientWidth : window.innerWidth;
        if (viewW <= SHEET_MAX) return true;
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
            // Пересечение цели с вьюпортом: крупные/подрезанные цели рисуем
            // только в видимой части, поповер якорится к ней же (r).
            r = rectInViewport(el.getBoundingClientRect(), viewW, viewH);
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
        // Цель исчезла из DOM или скрылась: ждём (DOM может дорисоваться), а
        // после MISSED_TICKS тиков подряд — закрываем тур. Раньше здесь был
        // skipForward(): тур автопрогонялся до конца и stop({mark:true})
        // СЖИГАЛ флаг «seen» ещё до первого жеста — wizard потом не
        // показывался сам никогда. Автозакрытие отметку не шлёт.
        if (!isVisible(querySel(step.sel))) {
            // show() ещё ждёт появления цели (хук открыл меню, переход едет) —
            // сдачу тура не считаем, иначе ожидание не успеет отработать.
            if (pendingWait) {
                missedTicks = 0;
                return;
            }
            missedTicks++;
            if (missedTicks >= MISSED_TICKS) {
                stop({ mark: false });
            }
            return;
        }
        missedTicks = 0;
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
            // keepalive: отметка по жесту пользователя — вкладка могла закрыться
            // сразу; fetch без keepalive браузер снимет при выгрузке страницы.
            keepalive: true,
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

        var resolved = resolveSteps(
            scopeName,
            conf.role ? String(conf.role) : "",
            conf.basics_seen === true
        );
        if (!resolved.length) return; // нечего показывать — тихо выходим

        try {
            // Юзер-туры локализуются на старте (правило триязычной
            // клиентской части): заменяем title/text без копирования
            // русской истины в таблицу.
            resolved = localizeUserSteps(scopeName, resolved);
        } catch (e) { /* ошибка локализации не валит тур */ }

        steps = resolved;
        cfg = {
            lang: ["chat", "solve", "find_error"].indexOf(scopeName) !== -1
                ? uiSuffix(wizardUiLanguage()) : "ru",
            scope: scopeName,
            version: conf.version,
            role: conf.role ? String(conf.role) : "",
            mark_url: conf.mark_url ? String(conf.mark_url) : ""
        };
        marked = false;
        interacted = false;
        missedTicks = 0;
        pendingWait = false;
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
        pendingWait = false;
        clearInterval(pollTimer);
        pollTimer = null;
        document.removeEventListener("scroll", onScroll, true);
        window.removeEventListener("resize", onScroll);
        document.removeEventListener("keydown", onKeydown);
        removeLayers();
        restoreState();
        // Отметка «seen» — только по жесту пользователя (Готово/Скрыть/Escape/
        // Далее) или если в туре он уже что-то нажимал. Автозакрытие без единого
        // жеста (цели исчезли до первого щелчка) флаг НЕ сжигает — иначе wizard
        // «показывался не сразу, а только по кнопке», прогорев до показа.
        // Перезапуск из кнопок (mark:false) отметку тоже не шлёт.
        var veto = opts && opts.mark === false;
        var force = opts && opts.mark === true;
        if (!veto && (force || interacted)) markSeen();
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
        return { scope: scope, version: 4, show: true, role: "", mark_url: "" };
    }

    function restartTour(conf, scopeOverride) {
        // Ручной запуск заменяет отложенный автостарт: иначе после закрытия
        // вручную запущенного тура автостарт запустил бы его снова.
        bootCancelled = true;
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

    // Слушатель всегда вешается на DOMContentLoaded (НЕ window.onload: страницы
    // сами его выставляют и затёрли бы наш обработчик), а вот сам автостарт
    // ждёт window.load — к нему успевает отработать и их init, и media-запросы
    // по zoom, и восстановление персистенса: тур стартует по готовой геометрии.
    // Слепые 400 мс после DOMContentLoaded могли опережать раскладку; цели
    // не находились, тур автопрогонялся до конца и СЖИГАЛ флаг «seen» (потом
    // «wizard показался не сразу, а только по кнопке»).
    function boot() {
        document.addEventListener("click", onDelegatedClick);
        var conf = readConfig();
        if (!conf || conf.show !== true) return;

        var autoStarted = false; // автостарт — ровно один за загрузку страницы
        function autoStart() {
            bootTimer = null;
            if (autoStarted || active || bootCancelled) return;
            autoStarted = true;
            start(conf.scope, conf);
        }

        if (document.readyState === "complete") {
            bootTimer = setTimeout(autoStart, READY_DELAY_MS);
            return;
        }
        window.addEventListener("load", function onWinLoad() {
            window.removeEventListener("load", onWinLoad);
            // Страховочный таймер больше не нужен.
            if (bootTimer) {
                clearTimeout(bootTimer);
                bootTimer = null;
            }
            bootTimer = setTimeout(autoStart, READY_DELAY_MS);
        });
        // Страховка: зависший ресурс (load не пришёл) не отменяет тур вовсе —
        // стартуем без него; если load догонит, autoStart упадёт в no-op.
        bootTimer = setTimeout(autoStart, MAX_BOOT_WAIT_MS);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    window.AIWizard = { start: start, stop: stop, isActive: isActive };
})();