// Сохранение состояния страницы админки: положение прокрутки, текст форм и
// фильтры. Состояние лежит в localStorage по ключу страницы и восстанавливается
// при следующем открытии.
//
// Фильтры — часть URL (?priority=high&kind=front&state=all): сервер рендерит
// таблицу по ним, поэтому восстановление идёт через переход на сохранённый
// URL (однократно, без цикла: после перехода query уже непустой).
//
// Общий хелпер для всех страниц (не только доски «Таски»): initAiPageState({
//   key: 'ai_tasks_page',   // ключ в localStorage
//   form: <form>,           // форма, поля которой сохраняем
//   fields: ['title', …],   // имена полей формы (без формы — не сохраняем)
//   scroll: true,           // сохранять ли положение прокрутки
// });
'use strict';

function initAiPageState(opts) {
    opts = opts || {};
    var key = opts.key;
    if (!key) return;
    var storageKey = 'ai_page_state:' + key;
    var form = opts.form || null;
    var fieldNames = opts.fields || [];
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
    if (form && state && state.fields) {
        fieldNames.forEach(function (name) {
            var el = form.querySelector('[name="' + name + '"]');
            if (!el) return;
            var saved = state.fields[name];
            if (saved === undefined || saved === null) return;
            if (el.value) return;
            el.value = saved;
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

    // Сохранение текущего состояния: query (фильтры), поля формы, скролл.
    function save() {
        var current = {
            query: window.location.search || '',
            scrollY: window.scrollY || 0,
            fields: {},
        };
        if (form) {
            fieldNames.forEach(function (name) {
                var el = form.querySelector('[name="' + name + '"]');
                if (el) current.fields[name] = el.value;
            });
        }
        writeState(current);
    }

    var timer = null;
    function scheduleSave() {
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(save, 300);
    }

    window.addEventListener('scroll', scheduleSave, { passive: true });
    window.addEventListener('beforeunload', save);
    if (form) {
        form.addEventListener('input', scheduleSave);
        form.addEventListener('change', scheduleSave);
    }
    // Смена фильтров = перезагрузка страницы: query обновится сам, но
    // сохраняем и немедленно, чтобы не потерять скролл.
    document.addEventListener('click', function (event) {
        if (event.target.closest('a, button, select')) save();
    });
}
