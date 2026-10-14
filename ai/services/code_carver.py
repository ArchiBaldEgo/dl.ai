"""Структурный экстрактор кодаdl.ai-прогонов (С-МПА / ассемблер i8086 / С-МПА-C).

Вариант B «по-взрослому»: вместо эвристик «самый длинный прогон код-строк» —
структурные маркеры языков курса. Слой services (CLAUDE.md: services
исполняют, раннеры оркестрируют): arm_runner делегирует сюда.

- i8086 (.i86/.asm): канонический каркас DL-программы
  ``jmp begin`` → данные (db/dw/dup/метки) → ``begin:`` → код →
  ``ends:`` / ``jmp ends`` / DL-маркер ``;$E``. Берём от первой строки-каркаса
  до последней строки конца (включая ``;$E``), хвостовая проза срезается.
- С-МПА (.mpc, псевдо-C): объявления ``__in``/``__out``/``__bits(...)``/``int main``
  … до парной закрывающей скобки ``}`` (хвостовая проза срезается).
- Гигиена оградок: строки ``` (целиком) и ```` ```lang ```` не считаются кодом
  и срезаются из результата (был кейс: ```` ```assembly ```` попадал в файл).

Все функции чистые (text → text|""), без обращений к БД/сети.
"""

import re as _re

_PROSE_WORD_RE = _re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
# (if/then/else/for/to/begin/end/do/with/uses/case, mov/idiv/…) в список НЕ
# входят, чтобы настоящий код с плотными операторами не браковался как проза.
_PROSE_STOPWORDS = frozenset({
    "we", "need", "needs", "the", "this", "that", "these", "those",
    "it", "its", "is", "are", "was", "were", "be", "been", "being",
    "our", "their", "they", "them", "have", "has", "had",
    "can", "could", "will", "would", "should", "must", "shall",
    "may", "might", "also", "but", "because", "which", "what",
    "how", "why", "when", "where", "there", "here", "into", "from",
    "per", "via", "please", "note", "just", "very", "more", "most",
    "some", "any", "each", "both", "one", "two", "now", "so", "all",
    "нужно", "нужен", "нужна", "если", "чтобы", "это", "этот", "эта",
    "как", "или", "также", "должен", "должна", "можно", "нельзя",
    "потом", "затем", "поэтому", "который", "которая", "быть", "было",
    "будут", "может", "наш", "наши", "они", "она", "его", "их",
    "для", "при", "всё", "все", "так", "вот", "есть", "там", "где",
    "когда", "почему", "какой",
})


def _looks_like_prose(text):
    """True, если текст похож на связную прозу (рассуждения модели), а не код.

    Порог подобран на реальном CoT (доля функциональных слов ≫ 0.15 при сотнях
    слов), а «голый» код и код с русскими комментариями долю не набирают.
    """
    words = _PROSE_WORD_RE.findall(text)
    if len(words) < 20:
        return False
    hits = sum(1 for w in words if w.lower() in _PROSE_STOPWORDS)
    return hits >= 12 and hits / len(words) >= 0.15

# Think-блоки модели (рассуждения): вырезаются целиком, вместе с содержимым —
# иначе strip_tags удаляет только разметку и рассуждения попадают в
# raw_response/«Извлечённый код программы» (модель может класть рассуждения
# прямо в ответ, а фолбэк ollama/sambanova возвращает thinking как ответ).
_THINK_RE = _re.compile(
    r"<think\b[^>]*>.*?(?:</think\s*>|\Z)",
    _re.DOTALL | _re.IGNORECASE,
)

# Маркеры «строка похожа на код»: синтаксис (скобка после идентификатора,
# ; { } =) и ключевые слова распространённых языков (Pascal/C/Python/asm).
# Нужен, чтобы текст без markdown-фенсов принимался как код только тогда,
# когда он и правда код, а не рассуждения модели (reasoning попадает в ответ
# целиком — например, фолбэк ollama/sambanova на thinking-поле).
_CODE_HINT_RE = _re.compile(
    r"\w\(|[;{}=]|\b(?:begin|end|program|var|const|procedure|function|mov|push|pop|jmp|"
    r"cmp|call|ret|int|imul|idiv|cbw|cwd|lea|xor|xchg|jge|jle|jne|jz|jnz|jg|jl|jb|ja|"
    r"include|import|def|class|print|writeln|printf|main|void|char|dw|db|dup|return)\b",
    _re.IGNORECASE,
)


def _strip_think_blocks(text):
    """Удалить think-блоки рассуждений вместе с содержимым."""
    if not text:
        return ""
    return _THINK_RE.sub("", text)


def _looks_like_code(text):
    """Похож ли текст без markdown-фенсов на код, а не на прозу-рассуждения.

    Доля строк с кодовыми маркерами (синтаксис/ключевые слова) должна быть
    ощутимой: у рассуждений она околонулевая, у кода — высокая.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    hints = sum(1 for line in lines if _CODE_HINT_RE.search(line))
    return hints >= 1 and hints / len(lines) >= 0.4


def _is_prose_sentence(line):
    """True, если строка — связное предложение (проза), а не строка кода.

    Строки CoT часто содержат редкие «кодовые» символы («Need determine
    sizes: a,b,RES word (2 bytes?); c,d byte.»), поэтому только `_CODE_HINT_RE`
    мало. Два признака прозы: (1) предложение, кончающееся точкой, — код
    кончается «;»/«}»/«end.», а у «end.» есть маркер «end»; (2) много словарных
    слов, из которых ощутимая доля — функциональные (EN/RU стоп-слова).
    """
    stripped = line.strip()
    # Точка в конце + связные слова — предложение (но «end.» держится на маркере).
    if stripped.endswith(".") and len(_PROSE_WORD_RE.findall(stripped)) >= 3:
        return True
    words = _PROSE_WORD_RE.findall(line)
    if len(words) < 5:
        return False
    hits = sum(1 for w in words if w.lower() in _PROSE_STOPWORDS)
    return hits / len(words) >= 0.3


def _line_is_code(line):
    """Код-подобная строка: есть `_CODE_HINT_RE`-маркер и это не проза."""
    if not _CODE_HINT_RE.search(line):
        return False
    return not _is_prose_sentence(line)


# Синтаксис, отсутствующий у хвостовых прощальных строк: конец оператора,
# блок, присваивание, Pascal-объявления/метки. Прощание («Удачи!») не содержит
# ни одного из них.
_CODE_SYNTAX_RE = _re.compile(r'[;{}=:]\s*$|[;{}=]')


def _is_trailing_prose(line):
    """True, если строка может стоять ПОСЛЕ кода как прощание/комментарий.

    Строки кода («end.», метки «L1:», «mov ax, 1») содержат маркер или синтаксис
    и не срезаются; строки без всего этого — прозаический мусор, ломающий
    компиляцию.
    """
    stripped = line.strip()
    if _CODE_HINT_RE.search(stripped) or _CODE_SYNTAX_RE.search(stripped):
        return False
    words = _PROSE_WORD_RE.findall(stripped)
    if not words:
        return True  # «—», «:)» и прочий мусор без букв
    return stripped.endswith((".", "!", "?")) or len(words) >= 4


def _trim_prose_tail(lines):
    """Срезать с конца списка строк хвостовой прозаический мусор.

    Модель после кода может добавить «Удачи!» («Вот и всё!») — без
    `_is_trailing_prose` такая строка попала бы в файл и сломала компиляцию
    на DL. Незакрытая при обрезке ответа строка кода обычно содержит
    маркеры/синтаксис и не срезается.
    """
    result = list(lines)
    while result and _is_trailing_prose(result[-1]):
        result.pop()
    return result



# Заголовок «второго решения»: после основного кода модели часто дают
# «Альтернатива:/Вариант 2:/Another solution:» и второй код-блок. Проза-гейты
# заголовок пропускают (короткие строки < 4 слов), а _carve_code_block
# «перепрыгивает» одиночную не-кодовую строку зазором ≤2 — без отдельного
# среза в сохранённое решение попадает и заголовок, и чужой код второй версии.
_ALT_HEADING_RE = _re.compile(
    r"^\s*(?:"
    r"альтернат\w*"
    r"|вариант\s*(?:№\s*)?\d"
    r"|вариант\s+[БбАа]?"
    r"|\d\s*[-—–]\s*[йя]?\s*вариант"
    r"|втор(?:ой|ая)\s+(?:вариант|версия|реализация|способ)"
    r"|alternative(?:\s+(?:variant|solution|approach|option|way))?"
    r"|another\s+(?:variant|solution|approach|option|way)"
    r"|second\s+(?:variant|solution|approach|option|way)"
    r"|способ\s+\d"
    r")[\s:—–-]*",
    _re.IGNORECASE,
)


def _is_alternative_heading(line):
    """True, если строка — заголовок второго варианта решения."""
    return bool(_ALT_HEADING_RE.match(line))


def cut_after_alternative_heading(text):
    """Срезать текст ПОСЛЕ первого заголовка «второго решения».

    Срез делается только когда до заголовка уже есть ≥2 кодовых индикатора
    (кодовая строка или строка-оградка): заголовок, открывающий ЕДИНСТВЕННЫЙ
    вариант («Вариант 1: …код»), код не трогает. Оградка считается — у
    инструкций вроде «sub ax, y» нет ни одного кодового маркера из
    _CODE_HINT_RE, а «mov + оградка» перед заголовком — уже основное решение.
    Возвращает укороченный текст (или исходный, если срезать нечего).
    """
    lines = text.splitlines()
    code_seen = 0
    for index, line in enumerate(lines):
        if _is_alternative_heading(line):
            if code_seen >= 2:
                return "\n".join(lines[:index]).rstrip()
        elif _line_is_code(line) or line.lstrip().startswith("```"):
            code_seen += 1
    return text


# Строка-оградка: ```, ```lang, отступы слева допустимы
_FENCE_LINE_RE = _re.compile(r"^\s*```[^\n]*$", _re.MULTILINE)

# --- i8086 asm ---
_JMP_BEGIN_RE = _re.compile(r"^\s*jmp\s+begin\b", _re.IGNORECASE)
_BEGIN_LABEL_RE = _re.compile(r"^\s*begin\s*:", _re.IGNORECASE)
# Концы i8086-программы: каркас DL (ends:/jmp ends + ;$E) и MASM-диалект
# («end start»); Паскаль-овский «end.» (без пробела+слова) не матчится.
_ASM_END_RE = _re.compile(r"^\s*(ends\s*:|jmp\s+ends\b|end\s+\w+\b)", _re.IGNORECASE)
_DL_END_MARKER_RE = _re.compile(r";\$E\b", _re.IGNORECASE)

# --- С-МПА (псевдо-C) ---
_MPA_DECL_RE = _re.compile(
    r"^\s*(?:int|void|char)\s+[A-Za-z_][\w\s]*\b(?:__in|__out|__bits\s*\()",
    _re.IGNORECASE,
)
_MPA_MAIN_RE = _re.compile(r"^\s*(?:int|void)\s+main\b", _re.IGNORECASE)
_CLOSING_BRACE_RE = _re.compile(r"^\s*}\s*;?\s*$")


def strip_code_fence_lines(text):
    """Срезать строки-оградки (```` ``` ```` / ```` ```lang ````) из CODE.

    Утечка оградки в сохранённый файл ломает компиляцию на DL (кейс res#5174).
    Только строчные оградки; код-строки с ``` внутри (незаконно, но не наше
    дело) не трогаем.
    """
    if not text:
        return ""
    cleaned = "\n".join(
        line for line in text.splitlines() if not _FENCE_LINE_RE.match(line)
    )
    return cleaned.strip()


# Строковый комментарий: // или ; или # в начале строки — часть кода (не
# считается проза-мусором при валидации середины кандидата).
# '*' намеренно НЕ в списке: markdown-буллеты CoT («*   Wait…») не должны
# маскироваться под комменты и обрывать детект рассуждений.
_COMMENT_START_RE = _re.compile(r"^\s*(?://|;+|#)")


def _mid_prose_lines(code):
    """ПРОЗА-предложения в СЕРЕДИНЕ кандидата кода (не первая/последняя строка).

    Хвостовую прозу срезает _strip_prose_tail; здесь ловим рассуждения модели,
    вставшие МЕЖДУ строками кода (реальный кейс res#3987: болванка-фенс из
    CoT + проза + настоящая программа — всё склеено одним кандидатом).
    Строки-комментарии (// ; #) — часть кода и мусором не считаются.
    Мусор: не-кодовая строка из ≥4 словарных слов (даже без точки — CoT-фразы
    «Need maybe `step`? Not») или явно прозя-предложение.
    """
    lines = [l for l in code.splitlines() if l.strip()]
    if len(lines) < 3:
        return []
    hits = []
    for line in lines[1:-1]:
        stripped = line.strip()
        if _COMMENT_START_RE.match(stripped):
            continue
        words = _PROSE_WORD_RE.findall(stripped)
        if len(words) < 4:
            continue
        if not _line_is_code(line) or _is_prose_sentence(line):
            hits.append(line)
    return hits


def _cut_mid_prose(code):
    """Вырезать проза-строки из середины кода (сохраняя каркас программы).

    Возвращает (cleaned_text, cut_count). Одиночная проза-вставка между
    объяв­лениями и main (или между инструкциями) не ломает компиляцию,
    если её вырезать; браковать весь кандидат — терять хороший код.
    Мусор-строка: не-коммент из ≥4 слов, не имеющая кодовых признаков
    (_line_is_code) или прямо прозя-предложение (точка + связные слова).
    """
    lines = list(code.splitlines())
    if len(lines) < 3:
        return code, 0
    kept = []
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 0 < i < len(lines) - 1 and stripped and _COMMENT_START_RE.match(stripped) is None:
            words = _PROSE_WORD_RE.findall(stripped)
            if len(words) >= 4 and (not _line_is_code(line) or _is_prose_sentence(line)):
                cut += 1
                continue
        kept.append(line)
    return "\n".join(kept), cut


def _last_index(pattern, text):
    last = None
    for m in pattern.finditer(text):
        last = m
    return last


def _strip_prose_tail(lines, keep_marker_re=None):
    """Срезать хвостовую прозу; если после кода идёт DL-маркер ``;$E`` — он
    остаётся (часть файла). Строки ``;$E`` держим всегда."""
    result = list(lines)
    while result:
        last = result[-1].strip()
        if not last:
            result.pop()
            continue
        if keep_marker_re is not None and keep_marker_re.search(last):
            break
        if _is_trailing_prose(last):
            result.pop()
            continue
        break
    return result


def _asm_candidates_for_anchor(lines, start):
    """Кандидат от анкера start до ближайшего концевого каркаса.

    ;$E сильнее (DL-маркер конца), ends:/jmp ends/end <метка> — минимум.
    ;$E внутри прозя-строки (CoT-цитата «…Ends: jmp Ends ;$E.») концом НЕ
    считается. Без концевика кандидата НЕТ (программа обрывается — либо
    болванка из рассуждений, либо модель дала каркас без конца). Возвращает
    (start, end, marker_seen) или None.
    """
    end = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _DL_END_MARKER_RE.search(line) and len(line.strip()) <= 60:
            # ;$E-носитель — короткая кодовая строка («jmp Ends ;$E»);
            # CoT-цитаты (простыни с «…Ends: jmp Ends ;$E. The user's…»)
            # концом программы не считаются.
            return (start, i, True)
        if _ASM_END_RE.match(line) and end is None:
            end = i  # продолжаем искать ;$E дальше, но ends: — минимум
    if end is None:
        return None
    return (start, end, False)


def extract_asm_i86(text, relaxed=False):
    """Структурный экстрактор ассемблера i8086 (DL-каркас с jmp begin).

    Возвращает код или "" (нет каркасных маркеров — не наш случай).

    Модели в CoT упоминают каркас и цитируют «болванки» программ; простая
    привязка к ПЕРВОМУ вхождению тащила болванку + прозу + программу скопом
    (res#3987). Поэтому: перебираем ВСЕ анкеры (^jmp begin), от каждого —
    кандидат до ближайшего концевика, валидируем, берём ПОСЛЕДНИЙ валидный
    (модели цитируют болванки раньше финального ответа).

    relaxed=True — строгие прозя-валидации (не-проза, мид-проза) пропускаются:
    аварийный тир «долгое рассуждение без фенсов», когда все строгие тиры
    дали пусто; структурные минимумы (концевик, ≥4 строк, один jmp begin)
    остаются, а вердикт определит DL. Вызывающий код применяет гейт
    has_language_marker.
    """
    if not text:
        return ""
    lines = [line for line in text.splitlines()]
    # Анкер — ТОЛЬКО «jmp begin»: строка «begin:» стоит ПОСЛЕ данных, и
    # привязка к ней отрезала бы дата-секцию (канд. begin:…Ends: без «n dw…»).
    anchors = [i for i, line in enumerate(lines) if _JMP_BEGIN_RE.match(line)]
    if not anchors:
        return ""
    candidates = []
    for start in anchors:
        span = _asm_candidates_for_anchor(lines, start)
        if span is None:
            # каркаса конца нет: возможно, модель писала без ;$E — пробуем
            # до конца текста, но только если у анкера РЕАЛЬНО есть данные
            # (dw/db/массив) и begin: ниже; иначе — болванка/проза.
            end = len(lines) - 1
            marker_seen = False
            has_data = any(_re.search(r"\b(?:dw|db|dup)\b", lines[j]) for j in range(start + 1, min(start + 15, len(lines))))
            has_begin = any(_BEGIN_LABEL_RE.match(lines[j]) for j in range(start + 1, len(lines)))
            if not (has_data and has_begin):
                continue
        else:
            _, end, marker_seen = span
        body = [l for l in lines[start:end + 1]]
        body = _strip_prose_tail(body, keep_marker_re=_DL_END_MARKER_RE)
        if marker_seen:
            idx = next((j for j, l in enumerate(body) if _DL_END_MARKER_RE.search(l)), None)
            if idx is not None:
                body = body[:idx + 1]
        candidate = strip_code_fence_lines("\n".join(body)).strip()
        if not candidate:
            continue
        if not relaxed and _looks_like_prose(candidate):
            continue
        cand_lines = candidate.splitlines()
        if len(cand_lines) < 4:
            continue
        # Повторный «jmp begin» внутри кандидата — склейка двух каркасов
        # (болванка из CoT + программа) — бракуем и в relaxed (res#3987):
        # такая склейка не компилируется ни при каком прозо-допущении.
        # Строки begin:/Ends: — законная часть каркаса.
        inner_jmp = sum(1 for j, l in enumerate(cand_lines) if j and _JMP_BEGIN_RE.match(l))
        if inner_jmp:
            continue
        if not relaxed:
            cut_code, _ = _cut_mid_prose(candidate)
            cut_code = strip_code_fence_lines(cut_code).strip()
            if not cut_code or _looks_like_prose(cut_code) or len(cut_code.splitlines()) < 4:
                continue
            candidate = cut_code
        candidates.append((marker_seen, candidate))
    if not candidates:
        return ""
    # Выбор: ПОСЛЕДНИЙ кандидат (модели пишут болванки/цитаты раньше финального
    # ответа, res#4160: программа дана дважды — берём чистый второй заход);
    # если есть кандидаты с ;$E — среди них берём последний.
    marked = [c for marker_seen, c in candidates if marker_seen]
    return (marked or [c for _, c in candidates])[-1]


def _closing_brace_line(line):
    """Строка, ЗАКРЫВАЮЩАЯ блочный кандидат: «}», «};» / «}» в хвосте строки.

    Скобка посреди строки (текст «{…}» в прозе) закрывающей не считается —
    окно должно кончаться на строке-скобке, как в настоящем коде.
    """
    stripped = (line.strip() or " ")
    return bool(_CLOSING_BRACE_RE.match(stripped) or _re.search(r"}\s*;?\s*$", stripped))


def extract_mpa_c(text, relaxed=False):
    """Структурный экстрактор С-МПА (псевдо-C с __in/__bits) и обычного C.

    ОКНО на каждый анкер (объявления __in/__out/__bits и int/void main):
    от анкера до БАЛАНСНОГО закрытия фигурных скобок (depth вернулся в 0),
    а НЕ до последней скобки всего текста — прежний вариант на длинном
    рассуждении собирал кандидата из ВСЕХ программ простыни (>1 main и дубли
    __bits) и браковал его целиком, оставляя пару без кода. Окно без «{» или
    с несошедшимся до конца текста балансом — отбрасывается. Валидации
    применяются к КАЖДОМУ окну (не проза, скобки сбалансированы, ≤1 main,
    дубли __bits — локально в окне); дубли программ МЕЖДУ окнами — норма
    (модель переписывает программу в рассуждении). Выбор: максимум строк,
    сортировка устойчива — при равенстве берётся ПОСЛЕДНИЙ (финальный ответ
    модели обычно позже; res#4160).

    relaxed=True — аварийный тир «долгое рассуждение без фенсов»: прозя-
    валидации (не-проза, мид-проза, _looks_like_code) и проверка дублей
    __bits пропускаются; окно с несколькими main обрезается до начала
    ПОСЛЕДНЕГО main; остаются только структурные минимумы (сбалансированные
    скобки, ≥4 строк). Вызывающий код применяет гейт has_language_marker.
    """
    if not text:
        return ""
    lines = [line for line in text.splitlines()]
    anchors = [i for i, line in enumerate(lines) if _MPA_DECL_RE.match(line) or _MPA_MAIN_RE.match(line)]
    if not anchors:
        return ""

    def window_end(start):
        """Первое БАЛАНСНОЕ закрытие скобок после анкера; None — не сошлось.

        Считаем depth по всем «{»/«}» строк; конец — строка-скобка
        (_closing_brace_line), на которой depth вернулся в 0 ПОСЛЕ того,
        как был > 0 (окно обязано что-то открыть).
        """
        depth = 0
        opened = False
        for i in range(start, len(lines)):
            stripped = lines[i].strip() or " "
            depth += stripped.count("{") - stripped.count("}")
            if depth > 0:
                opened = True
            if opened and depth <= 0 and _closing_brace_line(lines[i]):
                return i
        return None

    def window_end_from_decl(start):
        """Конец программы от якоря-объявления: declи + хелперы + main.

        Прежний window_end от якоря-объявления закрывался на ПЕРВОЙ
        сбалансированной функции: если модель оформила хелперы (abs_val/sign)
        ДО main, в кандидата попадали только declи с первым хелпером, main
        выпадал — DL падал («Undefined variable …»). Здесь окончание окна —
        закрытие блока main; хелперы ДО main считаются частью программы.
        Если main так и не закрылся — последний баланс до EOF.
        """
        depth = 0
        opened = False
        saw_main = False
        pending_end = None
        for i in range(start, len(lines)):
            stripped = lines[i].strip() or " "
            depth += stripped.count("{") - stripped.count("}")
            if depth > 0:
                opened = True
            if _MPA_MAIN_RE.match(lines[i]):
                saw_main = True
            if opened and depth <= 0 and _closing_brace_line(lines[i]):
                pending_end = i
                if saw_main:
                    return i
        return pending_end

    def end_for(start):
        """Якорь-объявление → до закрытия main (иначе теряются хелперы перед
        main); main-якорь → тело main, как раньше."""
        if _MPA_DECL_RE.match(lines[start]):
            return window_end_from_decl(start)
        return window_end(start)

    candidates = []
    for start in anchors:
        end = end_for(start)
        if end is None or end <= start:
            continue
        body = [l for l in lines[start:end + 1]]
        body = _strip_prose_tail(body)
        candidate = strip_code_fence_lines("\n".join(body)).strip()
        if not candidate:
            continue
        cand_line_count = len(candidate.splitlines())
        if cand_line_count < 4:
            continue
        if candidate.count("{") > candidate.count("}"):
            continue  # программа не закрыта — модель/болванка обрыв
        # Два main-а в окне — склейка двух болванок/программ; в строгом
        # режиме бракуем, в relaxed обрезаем окно до ПОСЛЕДНЕГО main.
        main_indices = [j for j, l in enumerate(candidate.splitlines()) if _MPA_MAIN_RE.match(l)]
        if len(main_indices) > 1:
            if not relaxed:
                continue
            truncated = "\n".join(candidate.splitlines()[main_indices[-1]:])
            truncated = strip_code_fence_lines(truncated).strip()
            if (
                truncated.splitlines()
                and len(truncated.splitlines()) >= 4
                and truncated.count("{") <= truncated.count("}")
            ):
                candidate = truncated
                cand_line_count = len(truncated.splitlines())
            else:
                continue
        if not relaxed:
            if _looks_like_prose(candidate):
                continue
            # Дубли С-МПА-деклараций (одна и та же __bits-переменная объявлена
            # дважды) — склеились объявления из болванки и программы (в
            # пределах ОДНОГО окна; между окнами дубли — норма).
            mpa_decl_names = _re.findall(
                r"\b[\w]+\s+(?:__in\s+|__out\s+|\s)*\s*__bits\s*\(\s*\d+\s*\)\s*[A-Za-z_]\w*",
                candidate,
                _re.IGNORECASE,
            )
            mpa_decl_norm = [_re.sub(r"\s+", " ", d).strip().lower() for d in mpa_decl_names]
            if len(mpa_decl_norm) != len(set(mpa_decl_norm)):
                continue
            cut_code, _ = _cut_mid_prose(candidate)
            cut_code = strip_code_fence_lines(cut_code).strip()
            if not cut_code or _looks_like_prose(cut_code):
                continue
            if not _looks_like_code(cut_code) and len(cut_code.splitlines()) < 4:
                continue
            candidate = cut_code
            cand_line_count = len(candidate.splitlines())
        candidates.append((cand_line_count, candidate))
    if not candidates:
        return ""
    # Лучший: больше строк (полный кандидат decls+main длиннее main-одного);
    # сортировка устойчива — при равенстве длина берётся ПОСЛЕДНИЙ.
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def has_mpa_markers(text):
    """Быстрый тест: похоже ли на С-МПА (для выбора экстрактора)."""
    if not text:
        return False
    return bool(
        _re.search(r"\b__bits\s*\(", text)
        or _re.search(r"\b__in\b", text)
        or _re.search(r"\b__out\b", text)
    )


def has_asm_markers(text):
    """Быстрый тест: похоже ли на ассемблер i8086 (jmp begin / begin: / ends)."""
    if not text:
        return False
    return bool(
        _JMP_BEGIN_RE.search(text)
        or _re.search(r"^\s*begin\s*:", text, _re.MULTILINE | _re.IGNORECASE)
        or _re.search(r"^\s*ends\s*:", text, _re.MULTILINE | _re.IGNORECASE)
    )


# --- Гейт мусора перед отправкой в DL ---
# Извлечённый код без ни одного структурного маркера языка курса — мусорный
# ответ (HYDRATION-JSON веб-пула, «[ВСТАВЬТЕ ВАШ КОД]», простыни рассуждений,
# проникшие в карвин): в DL такой текст только зря занимает очередь и
# плодит неинформативные «Ошибка компиляции (строка 1): window.HYDRATION…».
_ASM_GATE_RE = _re.compile(
    r"^\s*jmp\s+begin\b|^\s*begin\s*:|^\s*ends\s*:|jmp\s+ends\b|;\$E|"
    r"^\s*(?:\.model|\.stack|\.code|\.data)\b|^\s*org\s+\d+|\bmov\s+",
    _re.IGNORECASE | _re.MULTILINE,
)
_MPA_GATE_RE = _re.compile(r"\b__bits\s*\(|\b__in\b|\b__out\b|\bmain\s*\(", _re.IGNORECASE)


def has_language_marker(code, ext):
    """Есть ли в коде структурный маркер языка курса (.i86/.asm/.mpc).

    Для прочих расширений гейт не применяется (True). Используется воркером
    batch-solve перед отправкой на DL.
    """
    ext = (ext or "").strip().lower()
    if not code:
        return False
    if ext in (".i86", ".asm"):
        return bool(_ASM_GATE_RE.search(code))
    if ext == ".mpc":
        return bool(_MPA_GATE_RE.search(code))
    return True