// Сохранение состояния страницы админки: положение прокрутки, текст форм и
// фильтры. Состояние лежит в localStorage по ключу страницы и восстанавливается
// при следующем открытии.
//
// Общий хелпер для всех страниц (не только доски «Таски»):
//   initAiPageState({
//     key: 'ai_tasks_page',   // ключ в localStorage
//     form: <form>,           // форма, поля которой сохраняем
//     fields: ['title', …],   // имена полей формы (без формы — не сохраняем)
//     forms: [{form, fields}] // дополнительные формы (фильтры)
//     scroll: true,           // сохранять ли положение прокрутки
//   });
//
// Отдельно от static/admin/js/ai_admin_state.js: тот — механизм Django-
// changelist'ов (querystring per-pathname) и страниц-инструментов с ОДНОЙ
// формой; здесь — произвольное число форм на странице + прокрутка. Правка
// changelist'ов этим хелпером не ломается: он работает по явному ключу.
//
// Фильтры — часть URL (?kind=front&taken=…): сервер рендерит таблицу по ним,
// поэтому восстановление идёт через переход на сохранённый URL (однократно,
// без цикла: после перехода query уже непустой). Query обновляется заранее —
// по клику на ссылку с query и по сабмиту GET-формы (в момент клика
// location.search ещё старый).
'use strict';

function initAiPageState(opts) {
    opts = opts || {};
    var key = opts.key;
    if (!key) return;
    var storageKey = 'ai_page_state:' + key;
    var fieldSets = [];
    if (opts.form) {
        fieldSets.push({ form: opts.form, names: opts.fields || [] });
    }
    (opts.forms || []).forEach(function (item) {
        if (item && item.form) {
            fieldSets.push({ form: item.form, names: item.fields || [] });
        }
    });
    var wantScroll = opts.scroll !== false;

    function readState() {
        try {
            var raw = window.localStorage.getItem(storageKey);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }

    function writeState(state) {
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(state));
        } catch (e) {
            /* приватный режим / переполнение — не критично */
        }
    }

    var state = readState();

    // 1. Фильтры: сохранённый query восстанавливаем переходом на URL.
    if (state && state.query && !window.location.search) {
        window.location.replace(window.location.pathname + state.query);
        return;
    }

    // 2. Текст формы: восстанавливаем ТОЛЬКО если поле пустое (пользователь
    //    мог начать заполнять заново — не затираем его ввод).
    if (state && state.fields) {
        fieldSets.forEach(function (set) {
            set.names.forEach(function (name) {
                var el = set.form.querySelector('[name="' + name + '"]');
                if (!el) return;
                var saved = state.fields[name];
                if (saved === undefined || saved === null) return;
                if (el.value) return;
                el.value = saved;
            });
        });
    }

    // 3. Положение прокрутки — после того, как страница отрисовалась.
    if (state && wantScroll && typeof state.scrollY === 'number' && state.scrollY > 0) {
        var restore = function () {
            window.scrollTo(0, state.scrollY);
        };
        if (document.readyState === 'complete') {
            window.setTimeout(restore, 0);
        } else {
            window.addEventListener('load', function () { window.setTimeout(restore, 0); });
        }
    }

    // Query, который вот-вот будет в адресе: клик по ссылке с query или
    // сабмит GET-формы меняют фильтры — записываем их ДО перехода, иначе
    // сохранился бы старый (до перехода) набор фильтров.
    var pendingQuery = null;

    function currentQuery() {
        if (pendingQuery !== null) return pendingQuery;
        return window.location.search || '';
    }

    // Сохранение текущего состояния: query (фильтры), поля формы, скролл.
    function save() {
        if (window.__aiPageStateSkipSave) return;
        // Поля, которых нет на ТЕКУЩЕЙ странице (например, черновик формы
        // «Новый таск» — её нет на архивной вкладке), сохраняем как есть:
        // текущие значения перезаписывают, отсутствующие — остаются.
        var previous = readState();
        var fields = (previous && previous.fields) || {};
        fieldSets.forEach(function (set) {
            set.names.forEach(function (name) {
                var el = set.form.querySelector('[name="' + name + '"]');
                if (el) fields[name] = el.value;
            });
        });
        writeState({
            query: currentQuery(),
            scrollY: window.scrollY || 0,
            fields: fields,
        });
    }

    var timer = null;
    function scheduleSave() {
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(save, 300);
    }

    window.addEventListener('scroll', scheduleSave, { passive: true });
    window.addEventListener('beforeunload', save);
    fieldSets.forEach(function (set) {
        set.form.addEventListener('input', scheduleSave);
        set.form.addEventListener('change', scheduleSave);
    });

    // Ссылки: query новой страницы известна заранее (из href).
    document.addEventListener('click', function (event) {
        var link = event.target.closest('a[href]');
        if (link) {
            var href = link.getAttribute('href') || '';
            // Внешняя ссылка/скачивание — её query к состоянию страницы
            // отношения не имеет.
            var isLocal = href.indexOf('://') === -1
                && href.indexOf('javascript:') !== 0
                && href.indexOf('mailto:') !== 0;
            if (isLocal) {
                var q = href.indexOf('?') >= 0 ? href.slice(href.indexOf('?')) : '';
                pendingQuery = q || '';
            }
        }
        save();
    });

    // GET-формы (фильтры): собираем query из полей формы — на момент
    // сабмита location.search ещё старый.
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!form) return;
        var method = (form.getAttribute('method') || '').toLowerCase();
        if (method !== 'get') return;
        var params = new URLSearchParams();
        try {
            new FormData(form).forEach(function (value, key) {
                if (value !== '' && value !== null && typeof value !== 'object') {
                    params.append(key, value);
                }
            });
        } catch (e) {
            return;
        }
        var q = params.toString();
        pendingQuery = q ? '?' + q : '';
        save();
    });
}

// Сброс сохранённого состояния (после, например, создания таска —
// сохранённый черновик не должен воскреснуть). Флаг запрещает дальнейшие
// сохранения: иначе beforeunload при перезагрузке записал бы состояние
// обратно.
window.clearAiPageState = function (key) {
    window.__aiPageStateSkipSave = true;
    try {
        window.localStorage.removeItem('ai_page_state:' + key);
    } catch (e) {
        /* приватный режим — не критично */
    }
};
