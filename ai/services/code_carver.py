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

# Идентификатор — ОДНО слово: snake_case (`is_valid`, `has_flag`) не должен
# распадаться на стоп-слова `is/has`. Диапазон Latin-1 добавлен для FR-текста.
_PROSE_WORD_RE = _re.compile(
    r"[A-Za-zА-Яа-яЁёÀ-ÖØ-öø-ÿ][A-Za-zА-Яа-яЁёÀ-ÖØ-öø-ÿ0-9_]+"
)
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
    # FR (UI tri-язычный): чтобы простыня рассуждений не проходила как код.
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou",
    "est", "sont", "ce", "cette", "ces", "pour", "dans", "avec", "sur",
    "par", "que", "qui", "quoi", "comme", "plus", "moins", "nous",
    "vous", "ils", "elles", "il", "elle", "on", "se", "sa", "son",
    "ses", "leur", "leurs", "au", "aux", "en", "ne", "pas", "donc",
    "mais", "si", "alors", "lorsque", "où", "pourquoi", "comment",
    "être", "avoir", "faire", "peut", "doivent", "cela", "cet", "tout",
    "tous", "toute", "toutes", "aussi", "très", "bien", "afin", "entre",
    "sans", "sous", "vers", "chez", "car", "ni", "or",
})

# Регистры i8086 — омографы стоп-слов («si» исп./фр., «ds»/«es»/«ss»):
# в ассемблерной строке они часть операндов, а не проза.
_ASM_REGISTER_WORDS = frozenset({
    "si", "di", "ds", "es", "ss", "cs", "bp", "sp",
    "ax", "bx", "cx", "dx", "al", "ah", "bl", "bh", "cl", "ch", "dl", "dh",
})


def _looks_like_prose(text, ext=""):
    """True, если текст похож на связную прозу (рассуждения модели), а не код.

    Порог подобран на реальном CoT (доля функциональных слов ≫ 0.15 при сотнях
    слов), а «голый» код и код с русскими комментариями долю не набирают.

    При известном `ext` комментарии/строки маскируются: длинный docstring или
    `/* … */` — часть кода, его слова не должны раздувать проза-статистику.
    """
    if ext:
        view = "\n".join(_scan_comment_spans(text.splitlines(), ext).blanked)
    else:
        view = text
    words = _PROSE_WORD_RE.findall(view)
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
    r"include|import|def|class|print|writeln|printf|main|void|char|dw|db|dup|return|"
    # Verilog: однозначные слова (порт-листы ими и держатся)
    r"module|endmodule|wire|reg|logic|assign|always|posedge|negedge|"
    r"endcase|endfunction|endtask|timescale|"
    # C/C++/Java
    r"struct|typedef|extern|volatile|signed|unsigned|sizeof|define|ifdef|ifndef|"
    r"pragma|namespace|template|typename|nullptr|constexpr|operator|cout|cin|endl|"
    r"size_t|uint|static|abstract|extends|implements|instanceof|synchronized|"
    r"System|println|"
    # Python
    r"except|lambda|yield|assert|raise|nonlocal|async|await|isinstance|enumerate|"
    # Pascal
    r"downto|goto|constructor|destructor|inherited)\b",
    _re.IGNORECASE,
)

# Слова, которые в прозе встречаются слишком часто, чтобы быть глобальными
# маркерами (`for example`, `input data`, `a long time`). Для конкретного языка
# они — надёжный код; для прозы их отсекает `_is_prose_sentence`.
_C_FAMILY_HINT_RE = _re.compile(
    r"\b(?:if|for|while|switch|catch|do|else|try|new|delete|this|super|"
    r"throw|throws|union|auto|bool|byte|double|float|long|short|enum|"
    r"default|case)\b",
    _re.IGNORECASE,
)
_PY_HINT_RE = _re.compile(
    r"^\s*(?:if|elif|else|for|while|try|except|finally|with|def|class|"
    r"return|import|from|raise|yield|assert|global|nonlocal|del|pass|"
    r"break|continue|match|case)\b|^\s*@\w+|"
    r"\basync\s+(?:def|for|with)\b",
    _re.IGNORECASE,
)
_PAS_HINT_RE = _re.compile(
    r"\b(?:if|then|else|for|to|downto|do|while|repeat|until|case|record|"
    r"uses|array|readln|write|writeln|nil|mod|div|shl|shr|label|goto|"
    r"unit|interface|implementation|constructor|destructor|inherited)\b",
    _re.IGNORECASE,
)
_V_HINT_RE = _re.compile(
    r"\b(?:input|output|inout|parameter|localparam|generate|endgenerate|"
    r"genvar|case|initial|task|endtask|module|endmodule|wire|reg|logic|"
    r"assign|always|posedge|negedge|endcase)\b",
    _re.IGNORECASE,
)
# Мнемоники i8086, которых нет в _CODE_HINT_RE (там только mov/jmp/cmp/int/…).
# Без них строка «inc byte ptr cnt_c» (5 словарных слов, ни одного известного
# маркера) классифицировалась как проза и ВЫРЕЗАЛАСЬ из кода (GPT-OSS 120B,
# задача «гласные и согласные», живой прогон 2026-10-28). Инструкция считается
# кодом, только если после мнемоники есть операндный признак (запятая, скобка,
# цифра, «@», регистр, byte/word/ptr/offset/dup) или операндов нет вовсе
# (`nop`/`ret`/`hlt`): так проза «add the values» кодом не становится.
_ASM_MNEMONICS = (
    r"(?:inc|dec|add|adc|sub|sbb|mul|imul|div|idiv|neg|not|and|or|xor|test|"
    r"shl|shr|sal|sar|rol|ror|rcl|rcr|"
    r"mov|movsx|movzx|push|pop|pusha|popa|pushf|popf|xchg|lea|lds|les|"
    r"lodsb|lodsw|stosb|stosw|movsb|movsw|cmpsb|scasb|"
    r"cmp|jmp|je|jne|jz|jnz|jg|jge|jl|jle|ja|jae|jb|jbe|js|jns|jo|jno|"
    r"jc|jnc|jp|jnp|loop|loope|loopne|jcxz|"
    r"call|ret|retn|int|into|iret|nop|hlt|wait|"
    r"cbw|cwd|cwde|cdq|lahf|sahf|clc|stc|cmc|cld|std|cli|sti|"
    r"org|assume|segment|proc|endp|equ|db|dw|dd)"
)
_ASM_OPERAND_HINT = (
    r"[,\[\]\d@?'\"]|"
    r"\b(?:ax|bx|cx|dx|si|di|bp|sp|al|ah|bl|bh|cl|ch|dl|dh|"
    r"cs|ds|es|ss|byte|word|dword|ptr|offset|dup|xmm\d)\b"
)
# Метка i8086 (`begin:`, `loop_start:`) — тоже код, хотя ключевого слова нет.
_ASM_HINT_RE = _re.compile(
    r"^\s*[A-Za-z_]\w*\s*:\s*(?:;.*)?$"
    rf"|^\s*{_ASM_MNEMONICS}\b\s*(?:$|(?=[^\n]*{_ASM_OPERAND_HINT}))"
    # Ветвления/вызов: операнд — метка (`jae sort_done`, `call find_end`),
    # а не регистр/число. Идентификатор-операнд допустим ТОЛЬКО здесь:
    # «add the values» остаётся прозой.
    r"|^\s*(?:j\w+|call|loop\w*|jcxz)\s+[A-Za-z_]\w*\b",
    _re.IGNORECASE | _re.MULTILINE,
)

_HINT_BY_EXT = {
    ".c": _C_FAMILY_HINT_RE,
    ".cpp": _C_FAMILY_HINT_RE,
    ".mpc": _C_FAMILY_HINT_RE,
    ".java": _C_FAMILY_HINT_RE,
    ".py": _PY_HINT_RE,
    ".pas": _PAS_HINT_RE,
    ".v": _V_HINT_RE,
    ".i86": _ASM_HINT_RE,
    ".asm": _ASM_HINT_RE,
}


def _has_code_hint(line, ext=""):
    """Есть ли в строке кодовый маркер с учётом расширения."""
    if _CODE_HINT_RE.search(line):
        return True
    ext = (ext or "").strip().lower()
    pattern = _HINT_BY_EXT.get(ext)
    return bool(pattern and pattern.search(line))

# Хвостовой inline-комментарий строки кода: «// …» (С-МПА) или « ; …» (i86 —
# обязательно с пробелом ПЕРЕД «;»: `); c,d byte.` в CoT-строках и C-код вида
# «int a=0; b=1;» комментарием не являются; строка-комментарий целиком охраняется
# _COMMENT_START_RE).
_INLINE_TAIL_RE = _re.compile(r"//.*$|\s;.*$")

# Заголовок-подпись рассуждения (CoT): «And main function:», «Now the code:»,
# «Теперь функция:». Заканчивается двоеточием, ≥2 слова, без синтаксиса
# операторов. Метки и ветвления сюда НЕ попадают: метка — одно слово
# (loop_start:, begin:, Ends:), case/default/public исключены явно. Такой
# заголовок между декларациями и main ранее оставался в коде (слово «main»
# делает строку «кодовой» для _line_is_code) → DL падал «Ошибка синтаксиса».
_PROSE_HEADING_RE = _re.compile(
    r"^(?!\s*(?:case|default|public|private|protected)\b)"
    r"[^\s;{}()=/\[\]]+(?:\s+[^\s;{}()=/\[\]]+)+:\s*$",
    _re.IGNORECASE,
)

# --- Подписи модели, не являющиеся кодом (расширение _PROSE_HEADING_RE) ---
# Однословные и markdown-обёрнутые подписи («Код:», «**Код**:», «Функция main():»)
# прежний regex не ловил: он требовал ≥2 слов через пробел и двоеточие в самом
# конце строки. Такие строки оставались в окне между объявлениями и main →
# DL: «Ошибка синтаксиса». Одиночное EN-слово неотличимо от метки, поэтому
# срезаем его только по узкому белому списку; чисто кириллическое слово меткой
# быть не может (идентификаторы тулчейнов курса — ASCII).
_BULLET_PREFIX_RE = _re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
# `head` — всё до финального двоеточия; markdown-выделение (`**Код**:`,
# `` `Code`: ``, `**Код:**`) снимается отдельно.
_HEADING_LINE_RE = _re.compile(r"^(?P<head>.+?)[*_`]*\s*:\s*[*_`]*\s*$")
_MD_LEAD_RE = _re.compile(r"^[*_`]+")
_MD_TRAIL_RE = _re.compile(r"[*_`]+$")
# Синтаксис оператора/кавычки внутри «заголовка» — это код или строковый
# литерал (Python-ключ `"Код":`), а не подпись.
_HEADING_SYNTAX_RE = _re.compile(r"[;{}=/\[\]\"']")
# Ветвления/операторы, которые заканчиваются двоеточием, но кодом являются.
_HEADING_DENY_RE = _re.compile(
    r"^(?:case|default|public|private|protected|"
    r"if|elif|else|for|while|switch|def|class|function|procedure|"
    r"except|with|match|async\s+(?:def|for|with))\b",
    _re.IGNORECASE,
)
_HEADING_WORDS_EN = frozenset({
    "code", "solution", "answer", "listing", "implementation",
    "program", "result", "output", "final", "pseudocode", "algorithm",
})
# Каркасные имена i8086 — метки, их не срезаем никогда.
_STRUCT_LABEL_WORDS = frozenset({
    "begin", "end", "ends", "start", "main", "entry", "init", "go",
})
# Без контекста кандидата «Program:» консервативно считаем меткой.
_DEFAULT_HEADING_PROTECTED = _STRUCT_LABEL_WORDS | {"program"}
_CYRILLIC_RE = _re.compile(r"[А-Яа-яЁё]")
_ASCII_IDENT_RE = _re.compile(r"[A-Za-z0-9_]")

# Цели переходов/вызовов — метки, которые нельзя принять за заголовок даже
# если имя совпало с белым списком («je Code» → «Code:» остаётся).
_JUMP_MNEMONICS = (
    r"jmp|jo|jno|js|jns|je|jz|jne|jnz|jb|jnae|jc|jnb|jae|jnc|jbe|jna|"
    r"ja|jnbe|jl|jnge|jge|jnl|jle|jng|jg|jnle|jp|jpe|jnp|jpo|jcxz|jecxz"
)
_BRANCH_TARGET_RE = _re.compile(
    rf"^\s*(?:{_JUMP_MNEMONICS}|call|loop[a-z]*)\s+([A-Za-z_]\w*)\b",
    _re.IGNORECASE,
)
_ASM_END_LABEL_RE = _re.compile(r"^\s*end\s+([A-Za-z_]\w*)\b", _re.IGNORECASE)
# Адрес метки, взятый через offset/lea-операнд, тоже защищает её от среза.
_ADDRESS_LABEL_RE = _re.compile(
    r"\b(?:offset|lea\s+\w+\s*,)\s*([A-Za-z_]\w*)\b", _re.IGNORECASE,
)
# Метки C/Pascal: `goto Code;`, `&&Code` (GCC), паскалевское `label Code;`.
_GOTO_TARGET_RE = _re.compile(
    r"\b(?:goto|&&|break|continue)\s+([A-Za-z_]\w*)", _re.IGNORECASE,
)
_PASCAL_LABEL_DECL_RE = _re.compile(r"^\s*label\s+([^;]+);", _re.IGNORECASE)
_DATA_DIRECTIVE_RE = _re.compile(r"^\s*(?:dw|db|dd)\b(.*)$", _re.IGNORECASE)
_DATA_IDENT_RE = _re.compile(r"[A-Za-z_]\w*")


def _candidate_protected_labels(lines, ext=""):
    """Имена меток кандидата, которые нельзя принять за подпись.

    Защищаем реально используемые метки: цели jmp/je/…/call/loop*, «end <метка>»,
    ссылки из дата-секции и каркасные begin/ends/start/main/… Без этого
    однословная метка «Code:»/«Result:», на которую есть переход, была бы
    удалена как заголовок и программа перестала бы компилироваться.

    При известном `ext` текст комментариев маскируется: цитата «je Code» в
    рассуждении/комментарии не должна защищать подпись «Code:».
    """
    if ext:
        lines = _scan_comment_spans(lines, ext).blanked
    protected = set(_STRUCT_LABEL_WORDS)
    for line in lines:
        m = _BRANCH_TARGET_RE.match(line) or _ASM_END_LABEL_RE.match(line)
        if m:
            protected.add(m.group(1).lower())
        protected.update(tok.lower() for tok in _ADDRESS_LABEL_RE.findall(line))
        protected.update(tok.lower() for tok in _GOTO_TARGET_RE.findall(line))
        m = _PASCAL_LABEL_DECL_RE.match(line)
        if m:
            protected.update(tok.lower() for tok in _DATA_IDENT_RE.findall(m.group(1)))
        m = _DATA_DIRECTIVE_RE.match(line)
        if m:
            protected.update(tok.lower() for tok in _DATA_IDENT_RE.findall(m.group(1)))
    return protected


def _is_prose_heading_line(line, protected_labels=None):
    """True — строка является подписью-заголовком модели, а не кодом.

    Понимает «And main function:», «Функция main():», «Код:», «**Код**:»,
    «- Code:», «* Решение:». Метки (loop_start:, L1:, begin:, ends:, start:,
    case N:, default:), комментарии (// ; #) и строковые литералы не срезаются.
    """
    if not line:
        return False
    stripped = line.strip()
    if not stripped or _COMMENT_START_RE.match(stripped):
        return False
    stripped = _BULLET_PREFIX_RE.sub("", stripped).strip()
    m = _HEADING_LINE_RE.match(stripped)
    if not m:
        return False
    head = _MD_LEAD_RE.sub("", m.group("head")).strip()
    head = _MD_TRAIL_RE.sub("", head).strip()
    if not head or _HEADING_DENY_RE.match(head):
        return False
    if _HEADING_SYNTAX_RE.search(head):
        return False
    # Скобки допустимы только как хвостовая группа: «Функция main():».
    head = _re.sub(r"\s*\([^()]*\)\s*$", "", head).strip()
    if not head or "(" in head or ")" in head:
        return False
    protected = (
        _DEFAULT_HEADING_PROTECTED
        if protected_labels is None
        else protected_labels
    )
    if " " in head:
        # ≥2 слов: метка не может содержать пробел → подпись по прежней,
        # уже проверенной семантике.
        return bool(_PROSE_HEADING_RE.match(head + ":"))
    low = head.lower()
    if low in protected:
        return False
    if _CYRILLIC_RE.search(head) and not _ASCII_IDENT_RE.search(head):
        return True
    return low in _HEADING_WORDS_EN


def _strip_think_blocks(text):
    """Удалить think-блоки рассуждений вместе с содержимым."""
    if not text:
        return ""
    return _THINK_RE.sub("", text)


def _looks_like_code(text, ext=""):
    """Похож ли текст без markdown-фенсов на код, а не на прозу-рассуждения.

    Доля строк с кодовыми маркерами (синтаксис/ключевые слова) должна быть
    ощутимой: у рассуждений она околонулевая, у кода — высокая. Строки-
    комментарии не входят в знаменатель (иначе шапка из `//`/`/* */` топит
    валидный код), а строки с кодом и inline-комментарием считаются по
    маскированному виду — код в них остаётся.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if not ext:
        hints = sum(1 for line in lines if _CODE_HINT_RE.search(line))
        return hints >= 1 and hints / len(lines) >= 0.4
    scan = _scan_comment_spans(lines, ext)
    judged = [
        scan.blanked[i] for i in range(len(lines))
        if i not in scan.span_lines and scan.blanked[i].strip()
    ]
    if not judged:
        return False
    # Проза-защита: hint-строка, которая выглядит связным предложением,
    # считается кодом только при явном синтаксисе оператора (`; { } =`).
    # Иначе одно предложение рассуждений с языковым словом («… to …» для
    # Pascal, «… output …» для Verilog) проходило как код и отменяло
    # авто-ретрай. Синтаксисный гейт спасает однострочный код вроде
    # «program p; begin writeln(1); end.» (он тоже кончается точкой).
    def _hint_line(line):
        if not _has_code_hint(line, ext):
            return False
        if _is_prose_sentence(line, ext):
            return bool(_CODE_SYNTAX_RE.search(line))
        return True

    hints = sum(1 for line in judged if _hint_line(line))
    return hints >= 1 and hints / len(judged) >= 0.4


def _is_prose_sentence(line, ext="", in_comment=False):
    """True, если строка — связное предложение (проза), а не строка кода.

    Строки CoT часто содержат редкие «кодовые» символы («Need determine
    sizes: a,b,RES word (2 bytes?); c,d byte.»), поэтому только `_CODE_HINT_RE`
    мало. Признаки прозы: (1) предложение, кончающееся точкой, — код
    кончается «;»/«}»/«end.», а у «end.» есть маркер «end»; (2) заголовок-
    подпись («And main function:», «Now the code:»); (3) много словарных
    слов, из которых ощутимая доля — функциональные (EN/RU/FR стоп-слова).

    При известном `ext` комментарии/строки маскируются; строка, целиком
    состоящая из комментария/строкового литерала, прозой не считается
    (её разбирают отдельные гейты). Хвостовой inline-комментарий в legacy-
    режиме вырезается ПЕРЕД суждением по точке: строка кода с комментарием-
    предложением в конце — это КОД, срезка такой строки ломает компиляцию.
    """
    if in_comment:
        return False
    stripped = line.strip()
    if ext:
        judged = _blank_line(stripped, ext)
        if not judged.strip():
            return False
    else:
        m = _INLINE_TAIL_RE.search(stripped)
        judged = stripped[:m.start()].strip() if m else stripped
        if not judged:
            # Чистый комментарий («; Вводим данные здесь.») — судим по строке целиком.
            judged = stripped
    # Точка в конце + связные слова — предложение (но «end.» держится на маркере).
    if judged.endswith(".") and len(_PROSE_WORD_RE.findall(judged)) >= 3:
        return True
    # Заголовок-подпись рассуждения: «And main function:» — не код, хотя и
    # содержит слово main (иначе `_line_is_code` признаёт его кодом и строка
    # остаётся МЕЖДУ декларациями и main → DL «Ошибка синтаксиса»).
    if _is_prose_heading_line(judged):
        return True
    words = _PROSE_WORD_RE.findall(judged)
    if len(words) < 5:
        return False
    hits = sum(
        1 for w in words
        if w.lower() in _PROSE_STOPWORDS
        # Регистры i8086 — омографы стоп-слов («si» исп./фр. «если», «ds»/«es»):
        # «mov [arr + si], [arr + si - 1]» набирала 40% стоп-слов и ВЫРЕЗАЛАСЬ
        # как проза (Gemma 4, сортировка вставками, живой прогон 2026-10-29).
        and not (ext in (".i86", ".asm") and w.lower() in _ASM_REGISTER_WORDS)
    )
    return hits / len(words) >= 0.3


def _line_is_code(line, ext="", in_comment=False):
    """Код-подобная строка: есть кодовый маркер и это не проза."""
    if in_comment:
        return True
    if not ext:
        if not _CODE_HINT_RE.search(line):
            return False
        return not _is_prose_sentence(line)
    judged = _blank_line(line, ext)
    if not judged.strip():
        return False
    if not _has_code_hint(judged, ext):
        return False
    return not _is_prose_sentence(judged, ext)


# Синтаксис, отсутствующий у хвостовых прощальных строк: конец оператора,
# блок, присваивание, Pascal-объявления/метки. Прощание («Удачи!») не содержит
# ни одного из них.
_CODE_SYNTAX_RE = _re.compile(r'[;{}=:]\s*$|[;{}=]')


def _is_trailing_prose(line, protected_labels=None, ext="", in_comment=False):
    """True, если строка может стоять ПОСЛЕ кода как прощание/комментарий.

    Строки кода («end.», метки «L1:», «mov ax, 1») содержат маркер или синтаксис
    и не срезаются; строки без всего этого — прозаический мусор, ломающий
    компиляцию. Подпись-заголовок («Код:») проверяется ДО синтаксиса: двоеточие
    в конце — не признак кода в этом случае, а хвост заголовка.

    Блочные комментарии/docstring'и (`in_comment=True`) — часть кода и не
    срезаются. Строчные комментарии (`// …`, `; …`, `# …`) остаются
    срезаемыми, как раньше: прощальная фраза модели не должна попадать в файл.
    """
    if in_comment:
        return False
    stripped = line.strip()
    if _is_prose_heading_line(stripped, protected_labels):
        return True
    judged = _blank_line(stripped, ext) if ext else stripped
    if not judged.strip():
        # Строчный комментарий: судим по исходной строке (legacy-поведение).
        judged = stripped
    if _is_prose_sentence(judged, ext):
        return True
    if _has_code_hint(judged, ext) or _CODE_SYNTAX_RE.search(judged):
        return False
    words = _PROSE_WORD_RE.findall(judged)
    if not words:
        return True  # «—», «:)» и прочий мусор без букв
    return stripped.endswith((".", "!", "?")) or len(words) >= 4


def _trim_prose_tail(lines, ext=""):
    """Срезать с конца списка строк хвостовой прозаический мусор.

    Модель после кода может добавить «Удачи!» («Вот и всё!») — без
    `_is_trailing_prose` такая строка попала бы в файл и сломала компиляцию
    на DL. Незакрытая при обрезке ответа строка кода обычно содержит
    маркеры/синтаксис и не срезается. Блочный комментарий в хвосте — код.
    """
    result = list(lines)
    protected = _candidate_protected_labels(result, ext)
    scan = _scan_comment_spans(result, ext) if ext else None
    while result:
        idx = len(result) - 1
        if scan is not None and idx in scan.block_span_lines:
            break
        if _is_trailing_prose(result[-1], protected, ext=ext):
            result.pop()
            continue
        break
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


def _code_follows_heading(lines, index):
    """После заголовка «второго решения» СРАЗУ идёт код (оградка/код-строка)?

    Заголовок, открывающий вторую программу с ходу («Альтернатива:» →
    ```asm …), срезается; если между заголовком и кодом ещё прозовая строка
    («Альтернатива: нужно иначе. Финальная программа: …код»), срез не
    делается — иначе пропадала бы ФИНАЛЬНАЯ программа ниже (выкусывание).
    """
    for line in lines[index + 1:index + 4]:
        if not line.strip():
            continue
        return line.lstrip().startswith("```") or _line_is_code(line)
    return False


def _alternative_is_tail(lines, index):
    """После заголовка «второго решения» — ОДИН код-блок и больше кода нет.

    Заголовок может стоять ПОСРЕДИ рассуждения (GLM 5.3 Flash, живой прогон
    2026-10-28: «Альтернатива: xor/…» — фрагмент в ограде, а финальная
    программа ниже ещё через прозу и оградки). Срез по такому заголовку
    выбрасывал финальный ответ, и извлекался ранний черновик (`a db 250`
    вместо `a db 1`). Поэтому срезаем, только когда первый кодовый блок —
    последний код в ответе; дальше допустима лишь проза.
    """
    i = index + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return True
    if lines[i].lstrip().startswith("```"):
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith("```"):
            j += 1
        rest = lines[j + 1:] if j < len(lines) else []
    else:
        j = i
        while j < len(lines) and (not lines[j].strip() or _line_is_code(lines[j])):
            j += 1
        rest = lines[j:]
    return not any(l.strip().startswith("```") or _line_is_code(l) for l in rest)


def cut_after_alternative_heading(text):
    """Срезать текст ПОСЛЕ первого заголовка «второго решения».

    Срез делается только когда до заголовка уже есть ≥2 кодовых индикатора
    (кодовая строка или строка-оградка): заголовок, открывающий ЕДИНСТВЕННЫЙ
    вариант («Вариант 1: …код»), код не трогает. Оградка считается — у
    инструкций вроде «sub ax, y» нет ни одного кодового маркера из
    _CODE_HINT_RE, а «mov + оградка» перед заголовком — уже основное решение.
    Дополнительно код должен идти СРАЗУ после заголовка (_code_follows_heading)
    и это должен быть ПОСЛЕДНИЙ код ответа (_alternative_is_tail).
    Возвращает укороченный текст (или исходный, если срезать нечего).
    """
    lines = text.splitlines()
    code_seen = 0
    for index, line in enumerate(lines):
        if _is_alternative_heading(line):
            if (code_seen >= 2 and _code_follows_heading(lines, index)
                    and _alternative_is_tail(lines, index)):
                return "\n".join(lines[:index]).rstrip()
        elif _line_is_code(line) or line.lstrip().startswith("```"):
            code_seen += 1
    return text


# Строка-оградка: ```, ```lang, отступы слева допустимы
_FENCE_LINE_RE = _re.compile(r"^\s*```[^\n]*$", _re.MULTILINE)

# --- i8086 asm ---
_JMP_BEGIN_RE = _re.compile(r"^\s*jmp\s+begin\b", _re.IGNORECASE)
_BEGIN_LABEL_RE = _re.compile(r"^\s*begin\s*:", _re.IGNORECASE)
# Анкер каркасного перехода: «jmp begin» — канонический DL-каркас; модели
# разных провайдеров скашивают каркас по-своему (jmp start/start: — MASM,
# jmp begin_data/program/…), поэтому ЯКОРОМ считается ЛЮБОЙ «jmp <метка>»
# со СТАРТОВЫМ именем, у которого ниже есть одноимённая метка. Белый список
# стартовых имён обязателен: jmp на метке ЦИКЛА (jmp loop_start) якорем не
# становится — иначе хвостовое окно «съедает» дату-секцию программы.
_JUMP_ENTRY_RE = _re.compile(r"^\s*jmp\s+([A-Za-z_]\w*)\s*$", _re.IGNORECASE)
# Белый список стартовых имён — один владелец паттерна (анкер каркаса и
# нормализация склейки «проза+программа» ниже обязаны совпадать).
_ENTRY_LABEL_NAMES = r"(?:begin|start|prog\w*|program|main|init|entry|go)\w*"
_ENTRY_LABEL_RE = _re.compile(rf"^{_ENTRY_LABEL_NAMES}$", _re.IGNORECASE)
_LABEL_DEF_RE = _re.compile(r"^\s*([A-Za-z_]\w*)\s*:", )
# MASM-скелет без jmp-перехода вообще (.model/.stack/.data/.code/org 100h) —
# тоже якорь: от первой директивы до конца каркаса.
_MASM_DIR_RE = _re.compile(
    r"^\s*(?:\.model\b|\.stack\b|\.code\b|\.data\b|org\s+\d+"
    # Segment-диалект MASM (без .model): «assume cs:code, ds:data» и
    # «data segment»/«code segment». GLM 5.3 Flash отвечает именно так
    # (живой прогон 2026-10-28): без этих анкеров структурник не узнавал
    # программу, и карвинг терял «assume»/«data segment» — код без секции
    # данных не компилируется.
    r"|assume\s+[A-Za-z_]|[A-Za-z_]\w*\s+segment\b"
    # NASM-секции: «section .data» / «section .text» перед org 100h.
    # Gemma 4 отвечает так (живой прогон 2026-10-28): единственным анкером
    # оставался «org 100h», и кандидат начинался с него — секция данных
    # (n db 5 / f dw 0) выкусывалась, программа не компилируется.
    r"|section\s+\.\w+)",
    _re.IGNORECASE,
)
# Концы i8086-программы: каркас DL (ends:/jmp ends + ;$E) и MASM-диалект
# («end start», «end» без метки); Паскаль-овский «end.» (точка после слова)
# и проза «The end.» (слово ПЕРЕД end) не матчатся.
_ASM_END_RE = _re.compile(
    r"^\s*(ends\s*:|jmp\s+ends\b|end\s+\w+\b|end\s*$)", _re.IGNORECASE,
)
_DL_END_MARKER_RE = _re.compile(r";\$E\b", _re.IGNORECASE)
# ;$E, приклеенный к следующему тексту БЕЗ пробела («;$Ejmp begin»): веб-пул
# склеил размышления и финальный ответ (конвертер DOM потерял границу блоков).
# \b у канонического маркера такую склейку не ловит (E и j — оба \w), поэтому
# второй «jmp begin» не становится началом строки, а карвер тянет ОБЕ
# программы в одного кандидата — двойной код уходит в файл решения.
_DL_END_MARKER_GLUED_RE = _re.compile(r";\$E(?=\w)", _re.IGNORECASE)
# Программа, приклеенная к хвосту рассуждения БЕЗ перевода строки
# («…и сравнивай, if ajmp begin», «…Пример сходится.jmp begin»): конвертер
# пула/страницы склеил последнюю фразу CoT и финальный ответ. Анкер каркаса —
# строка ЦЕЛИКОМ («jmp begin»), поэтому финальная программа не находилась, и
# карвер тянул ранее процитированный черновик из CoT (логи #2606786/#2606762:
# в DL уходил фрагмент с русской прозой). `jmp` в середине строки в ассемблере
# невалиден, так что разрыв перед ним безопасен; список меток — стартовые
# имена, как у _ENTRY_LABEL_RE.
_ASM_ENTRY_GLUED_RE = _re.compile(
    rf"(?<=\S)(jmp\s+{_ENTRY_LABEL_NAMES})[ \t]*$",
    _re.IGNORECASE | _re.MULTILINE,
)


def normalize_dl_marker_glue(text):
    """Развести склейки «текст + код», потерявшие перевод строки в пуле.

    Два случая (оба — потеря границы блоков конвертером):

    * «jmp ends ;$Ejmp begin» → «… ;$E\\njmp begin» — иначе второй каркас не
      распознаётся отдельной программой (_DL_END_MARKER_GLUED_RE);
    * «…if ajmp begin» / «…сходится.jmp begin» → «…if a\\njmp begin» — иначе
      финальная программа не даёт анкера и карвер берёт черновик из CoT
      (_ASM_ENTRY_GLUED_RE; логи #2606786/#2606762).

    Обычный ответ не меняется: под оба предиката попадает только склейка.
    """
    if not text:
        return text
    text = _DL_END_MARKER_GLUED_RE.sub(lambda m: m.group(0) + "\n", text)
    return _ASM_ENTRY_GLUED_RE.sub(lambda m: "\n" + m.group(1), text)

# --- С-МПА (псевдо-C) ---
# Якорь-объявление С-МПА: ТРИ диалекта порядка слов —
#   старый DL-каркас:        «int N1 __in __bits(8);»
#   псевдо-C от моделей:     «unsigned char __in __bits(8) N1;»
#   псевдо-C без типа:       «unsigned __bits(3) x0;»
# — тип в начале (в т.ч. с префиксом unsigned), флаг __in/__out/__bits( дальше
# в любом месте. Второй порядок долгое время не матчится: декларации-порты не
# становились якорем, окно снимали по main, порты выпадали — «Undefined
# variable N1» на DL (лог задачи, queue 15211904). Третий диалект — глобальные
# переменные без имени типа: тоже не матчился, окно бралось по main, и
# глобальные объявления терялись (модель отдала «unsigned __bits(3) x0; …»,
# в файл ушло только «void main() {…}» — DL не видел переменных).
_MPA_DECL_RE = _re.compile(
    r"^\s*(?:"
    r"(?:unsigned\s+)?(?:int|void|char|short|long)\b"
    r".*(?:\b__in\b|\b__out\b|__bits\s*\()"
    r"|(?:unsigned\s+)?__bits\s*\(\s*\d+\s*\)\s*[A-Za-z_]\w*"
    r")",
    _re.IGNORECASE,
)
_MPA_MAIN_RE = _re.compile(r"^\s*(?:int|void)\s+main\b", _re.IGNORECASE)
_CLOSING_BRACE_RE = _re.compile(r"^\s*}\s*;?\s*$")
# Простое глобальное объявление без портов (``char A[100];``, ``int x;``):
# тип в начале, `;` в конце, без фигурных скобок. Такие строки — не якоря
# (в декларации нет __in/__out/__bits), и без них окно, начатое с якоря-
# объявления, теряло массивы входа ВЫШЕ него — «Undefined variable A» в DL.
_PLAIN_DECL_LINE_RE = _re.compile(
    r"^\s*(?:const\s+|static\s+)*(?:unsigned\s+|signed\s+)*"
    r"(?:int|char|short|long|float|double)\b[^;{}]*;\s*(?://.*)?$",
    _re.IGNORECASE,
)
# Имя переменной в объявлении __bits (для проверки дублей на файловом уровне).
_MPA_DECL_NAME_RE = _re.compile(
    r"\b[\w]+\s+(?:__in\s+|__out\s+|\s)*\s*__bits\s*\(\s*\d+\s*\)\s*[A-Za-z_]\w*",
    _re.IGNORECASE,
)


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


# --- Языко-зависимые спаны комментариев/строк -------------------------------
# Блочные комментарии/докстринги: (открыватель, закрыватель).
_C_BLOCK_DELIMS = (("/*", "*/"),)
_BLOCK_COMMENT_DELIMS = {
    ".c": _C_BLOCK_DELIMS,
    ".cpp": _C_BLOCK_DELIMS,
    ".java": (("/*", "*/"), ('"""', '"""')),  # Java text blocks
    ".v": _C_BLOCK_DELIMS,
    ".mpc": _C_BLOCK_DELIMS,
    ".i86": _C_BLOCK_DELIMS,
    ".asm": _C_BLOCK_DELIMS,
    ".pas": (("/*", "*/"), ("{", "}"), ("(*", "*)")),
    ".py": (('"""', '"""'), ("'''", "'''")),
}
_DEFAULT_BLOCK_DELIMS = (("/*", "*/"), ('"""', '"""'), ("'''", "'''"))

# Строчные комментарии. Для C `#` — не комментарий (это #include).
_LINE_COMMENT_PREFIXES = {
    ".py": ("#",),
    ".c": ("//",), ".cpp": ("//",), ".java": ("//",), ".v": ("//",),
    ".mpc": ("//",), ".pas": ("//",),
    ".i86": (";",), ".asm": (";",),
}
_DEFAULT_LINE_COMMENT_PREFIXES = ("//", "#", ";")

# Строковые литералы: (кавычка, многострочная?). Для .v апостроф — разделитель
# разрядов в based-литералах (`4'b1010`), поэтому строкой не считается.
_STRING_QUOTES = {
    ".py": (('"""', True), ("'''", True), ('"', False), ("'", False)),
    ".pas": (("'", False),),
    ".v": (('"', False),),
    ".c": (('"', False), ("'", False)),
    ".cpp": (('"', False), ("'", False)),
    ".java": (('"""', True), ('"', False), ("'", False)),
    ".mpc": (('"', False), ("'", False)),
    ".i86": (('"', False), ("'", False)),
    ".asm": (('"', False), ("'", False)),
}
_DEFAULT_STRING_QUOTES = (('"""', True), ("'''", True), ('"', False), ("'", False))

# Префикс Python-докстринга (`r"""`, `f'''`): его буква — не код.
_DOC_PREFIX_RE = _re.compile(r"[rRuUbBfF]{1,2}")


class _CommentScan:
    """Результат разметки комментариев/строк для одного списка строк."""

    __slots__ = ("span_lines", "block_span_lines", "blanked")

    def __init__(self, span_lines, block_span_lines, blanked):
        self.span_lines = span_lines              # строки целиком внутри спана
        self.block_span_lines = block_span_lines  # блочные/строковые (не //)
        self.blanked = blanked                    # те же строки с пробелами


def _scan_comment_spans(lines, ext=""):
    """Разметить комментарии/строки: (span_lines, block_span_lines, blanked).

    `span_lines` — строки, у которых ВСЕ непробельные символы внутри
    комментария/строкового литерала; их нельзя резать как прозу. `blanked` —
    те же строки, где содержимое комментариев/строк заменено пробелами: по
    нему считаются кодовые маркеры и проза-слова (inline-комментарий не топит
    строку кода). `block_span_lines` — подмножество span_lines для блочных
    комментариев/докстрингов/строковых литералов; строчные комментарии сюда
    не входят, чтобы хвостовое «// Удачи!» по-прежнему срезалось.

    Незакрытый блочный комментарий/тройная строка тянется до EOF (невалидный
    код и так не скомпилируется). Одиночная строка без закрывающей кавычки
    сбрасывается в конце строки, если строка не кончается на `\\`.
    """
    ext = (ext or "").strip().lower()
    block_delims = _BLOCK_COMMENT_DELIMS.get(ext, _DEFAULT_BLOCK_DELIMS)
    line_prefixes = _LINE_COMMENT_PREFIXES.get(ext, _DEFAULT_LINE_COMMENT_PREFIXES)
    string_quotes = _STRING_QUOTES.get(ext, _DEFAULT_STRING_QUOTES)

    span_lines = set()
    block_span_lines = set()
    blanked = []
    state = None  # None | ("block", closer) | ("string", quote, triple)

    for idx, raw in enumerate(lines):
        text = raw
        out = []
        has_span = False
        has_code = False
        block_span = False
        i = 0
        while i < len(text):
            if state is not None:
                kind = state[0]
                if kind == "block":
                    closer = state[1]
                    pos = text.find(closer, i)
                    if pos < 0:
                        out.append(" " * (len(text) - i))
                        has_span = True
                        block_span = True
                        i = len(text)
                        break
                    out.append(" " * (pos + len(closer) - i))
                    has_span = True
                    block_span = True
                    i = pos + len(closer)
                    state = None
                    continue

                quote, triple = state[1], state[2]
                if triple:
                    pos = text.find(quote, i)
                    if pos < 0:
                        out.append(" " * (len(text) - i))
                        has_span = True
                        block_span = True
                        i = len(text)
                        break
                    out.append(" " * (pos + len(quote) - i))
                    has_span = True
                    block_span = True
                    i = pos + len(quote)
                    state = None
                    continue

                closed = False
                while i < len(text):
                    ch = text[i]
                    if ch == "\\":
                        out.append(" ")
                        if i + 1 < len(text):
                            out.append(" ")
                            i += 2
                        else:
                            i += 1
                        has_span = True
                        block_span = True
                        continue
                    if (ext == ".pas" and ch == "'" and i + 1 < len(text)
                            and text[i + 1] == "'"):
                        out.append("  ")
                        i += 2
                        has_span = True
                        block_span = True
                        continue
                    if ch == quote:
                        out.append(" ")
                        i += 1
                        has_span = True
                        block_span = True
                        state = None
                        closed = True
                        break
                    out.append(" ")
                    i += 1
                    has_span = True
                    block_span = True
                if not closed and not text.rstrip().endswith("\\"):
                    state = None
                continue

            # state is None: блочный комментарий → строка/кавычка → // ; #
            hit = None
            for opener, closer in block_delims:
                if text.startswith(opener, i):
                    hit = (opener, closer)
                    break
            if hit is not None:
                opener, closer = hit
                has_span = True
                block_span = True
                pos = text.find(closer, i + len(opener))
                if pos < 0:
                    out.append(" " * (len(text) - i))
                    state = ("block", closer)
                    i = len(text)
                    break
                out.append(" " * (pos + len(closer) - i))
                i = pos + len(closer)
                continue

            hitq = None
            for quote, triple in string_quotes:
                if text.startswith(quote, i):
                    hitq = (quote, triple)
                    break
            if hitq is not None:
                quote, triple = hitq
                has_span = True
                block_span = True
                out.append(" " * len(quote))
                i += len(quote)
                if triple:
                    pos = text.find(quote, i)
                    if pos < 0:
                        out.append(" " * (len(text) - i))
                        state = ("string", quote, True)
                        i = len(text)
                        break
                    out.append(" " * (pos + len(quote) - i))
                    i = pos + len(quote)
                else:
                    state = ("string", quote, False)
                continue

            hitc = None
            for prefix in line_prefixes:
                if text.startswith(prefix, i):
                    # `;` — комментарий только в начале строки/после пробела:
                    # `int a=0; b=1;` — не комментарий.
                    if prefix == ";" and i > 0 and not text[i - 1].isspace():
                        continue
                    hitc = prefix
                    break
            if hitc is not None:
                has_span = True
                out.append(" " * (len(text) - i))
                i = len(text)
                break

            ch = text[i]
            out.append(ch)
            if not ch.isspace():
                has_code = True
            i += 1

        blanked_line = "".join(out)
        # `r"""…"""`: буква-префикс не делает строку кодовой.
        if has_span and has_code and ext == ".py":
            if _DOC_PREFIX_RE.fullmatch(blanked_line.strip()):
                has_code = False
        if has_span and not has_code:
            span_lines.add(idx)
            if block_span:
                block_span_lines.add(idx)
        blanked.append(blanked_line)

    return _CommentScan(
        frozenset(span_lines), frozenset(block_span_lines), blanked,
    )


def _comment_span_lines(lines, ext=""):
    """Только индексы строк, целиком занятых комментарием/строкой."""
    return _scan_comment_spans(lines, ext).span_lines


def _blank_line(line, ext=""):
    """Строка с содержимым комментариев/строк, заменённым пробелами."""
    return _scan_comment_spans([line], ext).blanked[0]


def _mid_prose_lines(code, ext=""):
    """ПРОЗА-предложения в СЕРЕДИНЕ кандидата кода (не первая/последняя строка).

    Хвостовую прозу срезает _strip_prose_tail; здесь ловим рассуждения модели,
    вставшие МЕЖДУ строками кода (реальный кейс res#3987: болванка-фенс из
    CoT + проза + настоящая программа — всё склеено одним кандидатом).
    Строки-комментарии (// ; # / блочные / докстринги) — часть кода и мусором
    не считаются. Мусор: не-кодовая строка из ≥4 словарных слов (даже без
    точки — CoT-фразы «Need maybe `step`? Not»), заголовок-подпись
    («And main function:») или явно прозя-предложение.
    """
    lines = [l for l in code.splitlines() if l.strip()]
    if len(lines) < 3:
        return []
    scan = _scan_comment_spans(lines, ext) if ext else None
    hits = []
    for idx in range(1, len(lines) - 1):
        line = lines[idx]
        stripped = line.strip()
        if scan is not None:
            if idx in scan.span_lines:
                continue
            judged = scan.blanked[idx]
        else:
            if _COMMENT_START_RE.match(stripped):
                continue
            judged = stripped
        words = _PROSE_WORD_RE.findall(judged)
        if len(words) < 4 and not _PROSE_HEADING_RE.match(judged.strip()):
            continue
        if not _line_is_code(judged, ext) or _is_prose_sentence(judged, ext):
            hits.append(line)
    return hits


def _cut_mid_prose(code, ext=""):
    """Вырезать проза-строки из середины кода (сохраняя каркас программы).

    Возвращает (cleaned_text, cut_count). Одиночная проза-вставка между
    объяв­лениями и main (или между инструкциями) не ломает компиляцию,
    если её вырезать; браковать весь кандидат — терять хороший код.
    Мусор-строка: не-коммент из ≥4 слов (или заголовок-подпись, даже короткий),
    не имеющая кодовых признаков (_line_is_code) или прямо прозя-предложение
    (точка + связные слова). Комментарии/докстринги/строки не режутся.
    """
    lines = list(code.splitlines())
    if len(lines) < 3:
        return code, 0
    scan = _scan_comment_spans(lines, ext) if ext else None
    protected = _candidate_protected_labels(lines, ext)
    kept = []
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 0 < i < len(lines) - 1 and stripped:
            if scan is not None:
                if i in scan.span_lines:
                    kept.append(line)
                    continue
                judged = scan.blanked[i]
            else:
                if _COMMENT_START_RE.match(stripped):
                    kept.append(line)
                    continue
                judged = stripped
            words = _PROSE_WORD_RE.findall(judged)
            # Подпись режется даже при 1-2 словах: «Program:» ошибочно выглядит
            # кодом для _line_is_code из-за ключевого слова, но остаётся мусором.
            if _is_prose_heading_line(stripped, protected) or (
                    len(words) >= 4 and (
                        not _line_is_code(judged, ext)
                        or _is_prose_sentence(judged, ext))):
                cut += 1
                continue
        kept.append(line)
    return "\n".join(kept), cut


def _last_index(pattern, text):
    last = None
    for m in pattern.finditer(text):
        last = m
    return last


def _strip_prose_tail(lines, keep_marker_re=None, ext=""):
    """Срезать хвостовую прозу; если после кода идёт DL-маркер ``;$E`` — он
    остаётся (часть файла). Строки ``;$E`` держим всегда. Блочный
    комментарий/docstring в хвосте — часть кода, не срезается."""
    result = list(lines)
    protected = _candidate_protected_labels(result, ext)
    scan = _scan_comment_spans(result, ext) if ext else None
    while result:
        idx = len(result) - 1
        last = result[-1].strip()
        if not last:
            result.pop()
            continue
        if scan is not None and idx in scan.block_span_lines:
            break
        if keep_marker_re is not None and keep_marker_re.search(last):
            break
        if _is_trailing_prose(last, protected, ext=ext):
            result.pop()
            continue
        break
    return result


def strip_prose_heading_lines(text, ext=""):
    """Удалить строки-подписи модели из готового кода (в любой позиции).

    Заголовок не является кодом ни в одном тире, включая relaxed (там
    пропускаются прозя-валидации, но подпись гарантированно ломает компиляцию
    DL). Метки, ветвления и комментарии защищены `_is_prose_heading_line`;
    строки внутри блочных комментариев/докстрингов не трогаются вовсе.
    Возвращает текст без изменений, если срезать нечего.
    """
    if not text:
        return ""
    lines = text.splitlines()
    scan = _scan_comment_spans(lines, ext) if ext else None
    protected = _candidate_protected_labels(lines, ext)
    kept = []
    for i, line in enumerate(lines):
        if scan is not None and i in scan.span_lines:
            kept.append(line)
            continue
        if not _is_prose_heading_line(line, protected):
            kept.append(line)
    return "\n".join(kept)


# Прелюдия программы i8086 — строки, которые обязаны остаться в коде, даже
# если анкером стал `jmp <стартовая метка>` НИЖЕ них (DOS-стиль
# `org 100h` → данные → `jmp start`): GPT-OSS 120B, живой прогон 2026-10-28.
_ASM_PRELUDE_RE = _re.compile(
    r"^\s*(?:org\s+\d+|section\s+\.\w+|\.(?:model|stack|data|code|bss|186|286)\b"
    r"|assume\b|[A-Za-z_]\w*\s+segment\b"
    r"|[A-Za-z_]\w*\s+(?:db|dw|dd)\b"
    r"|(?:db|dw|dd)\s"
    r"|[A-Za-z_]\w*\s+equ\b)",
    _re.IGNORECASE,
)


def _asm_prelude_start(lines, start, ext=".asm"):
    """Сдвинуть начало кандидата вверх по прелюдии (org/section/данные)."""
    scan = _scan_comment_spans(lines, ext) if ext else None
    judged = scan.blanked if scan is not None else lines
    i = start
    while i > 0:
        prev = judged[i - 1]
        if not prev.strip() or _ASM_PRELUDE_RE.match(prev):
            i -= 1
            continue
        break
    return i


def _asm_candidates_for_anchor(lines, start, ext=".asm"):
    """Кандидат от анкера start до ближайшего концевого каркаса.

    ;$E сильнее (DL-маркер конца), ends:/jmp ends/end <метка> — минимум.
    ;$E внутри прозя-строки (CoT-цитата «…Ends: jmp Ends ;$E.») концом НЕ
    считается. Без концевика кандидата НЕТ (программа обрывается — либо
    болванка из рассуждений, либо модель дала каркас без конца). Возвращает
    (start, end, marker_seen) или None.

    `;$E` — специальный строчный комментарий DL: его ищем по СЫРОЙ строке
    (маскирование комментариев его бы стёрло), а `ends:` — по маскированной,
    чтобы цитата в комментарии не закрыла программу.
    """
    scan = _scan_comment_spans(lines, ext) if ext else None
    end = None
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if _DL_END_MARKER_RE.search(line) and len(line.strip()) <= 60:
            # ;$E-носитель — короткая кодовая строка («jmp Ends ;$E»);
            # CoT-цитаты (простыни с «…Ends: jmp Ends ;$E. The user's…»)
            # концом программы не считаются.
            return (start, i, True)
        judged = scan.blanked[i] if scan is not None else line
        if _ASM_END_RE.match(judged) and end is None:
            end = i  # продолжаем искать ;$E дальше, но ends: — минимум
    if end is None:
        return None
    return (start, end, False)


def _asm_anchors(lines, ext=".asm"):
    """Индексы анкеров каркаса i8086: «jmp <стартовая метка>» с одноимённой
    меткой ниже, а при их отсутствии — MASM-директивы (.model/.stack/.data/
    .code/org 100h). «begin:» и метки циклов якорями не считаются: привязка
    к линии МЕТКИ отрезала бы дату-секцию, а цикл-метка «съедает» программу
    хвостовым окном. Комментарии маскируются: `jmp begin` в `; …` — не анкер.

    Анкер — только ПРЯМОЙ переход (метка объявлена НИЖЕ перехода), как у
    `jmp begin` через секцию данных. Обратный переход на метку цикла
    (`jmp main_loop`, где `main_loop:` выше) якорем не является — иначе
    эвристика имени (`main\\w*`) принимала цикл-метку за точку входа, и
    выкушенный «кандидат» начинался с `jmp main_loop`, теряя данные и начало
    программы (DL: «Неопределён символ main_loop, строка 1»).
    """
    scan = _scan_comment_spans(lines, ext) if ext else None
    judged = scan.blanked if scan is not None else lines
    label_lines = {}
    for i, line in enumerate(judged):
        m = _LABEL_DEF_RE.match(line)
        if m:
            label_lines.setdefault(m.group(1).lower(), []).append(i)
    anchors = []
    for i, line in enumerate(judged):
        m = _JUMP_ENTRY_RE.match(line)
        if not m or not _ENTRY_LABEL_RE.match(m.group(1)):
            continue
        label = m.group(1).lower()
        # Прямой переход: определение метки строго ниже перехода. Список
        # (а не одна строка) — программы могут повторяться (`jmp begin` и
        # `begin:` во втором заходе), и каждый заход обязан остаться анкером.
        if any(def_line > i for def_line in label_lines.get(label, ())):
            anchors.append((i, label))
    if not anchors:
        # MASM-скелет: только ПЕРВАЯ директива (каждая следующая — якорь
        # хвостового окна, которые «съедали» дату-секцию: .code-анкер давал
        # кандидата без .model/.stack/.data).
        for i, line in enumerate(judged):
            if _MASM_DIR_RE.match(line):
                anchors.append((i, None))
                break
    return anchors


def _asm_candidates(lines, relaxed=False, ext=".asm"):
    """Валидные кандидаты-программы i8086: ``(marker_seen, start, end, code)``.

    ``start``/``end`` — индексы строк исходного текста (включительно): от
    анкера-перехода до концевика каркаса; ``code`` — уже очищенный текст
    программы. Один проход нужен и экстрактору (выбор последнего), и
    нормализации «Полного ответа модели» (обрезка склеенного черновика) —
    правила валидации у них обязаны совпадать.
    """
    anchors = _asm_anchors(lines, ext)
    if not anchors:
        return []
    scan = _scan_comment_spans(lines, ext) if ext else None
    judged = scan.blanked if scan is not None else lines
    candidates = []
    for start, anchor_label in anchors:
        start = _asm_prelude_start(lines, start, ext)
        span = _asm_candidates_for_anchor(lines, start, ext)
        if span is None:
            # Каркаса конца нет: возможно, модель писала без ;$E/обрезал
            # ответ — пробуем до конца текста, но только если у анкера ниже
            # РЕАЛЬНО есть данные (dw/db/dup, скан по всему хвосту —
            # 15-строчный кап терял длинные секции данных) и метка каркаса;
            # MASM-директиве достаточно самой секции .data/.code ниже.
            end = len(lines) - 1
            marker_seen = False
            data_re = _re.compile(r"\b(?:dw|db|dup)\b", _re.IGNORECASE)
            has_data = any(data_re.search(judged[j]) for j in range(start + 1, len(lines)))
            if anchor_label is not None:
                has_label = any(
                    _re.match(
                        rf"^\s*{_re.escape(anchor_label)}\s*:",
                        judged[j], _re.IGNORECASE,
                    )
                    for j in range(start + 1, len(lines))
                )
                if not (has_data and has_label):
                    continue
            else:
                # MASM-директива: ниже достаточно любых данных или меток
                # (`.data`/`.code` секция может и не иметь dw/db вовсе).
                has_any = any(
                    data_re.search(judged[j]) or _LABEL_DEF_RE.match(judged[j])
                    for j in range(start + 1, len(lines))
                )
                if not has_any:
                    continue
        else:
            _, end, marker_seen = span
        body = [l for l in lines[start:end + 1]]
        body = _strip_prose_tail(body, keep_marker_re=_DL_END_MARKER_RE, ext=ext)
        if marker_seen:
            idx = next((j for j, l in enumerate(body) if _DL_END_MARKER_RE.search(l)), None)
            if idx is not None:
                body = body[:idx + 1]
        candidate = strip_code_fence_lines("\n".join(body)).strip()
        if not candidate:
            continue
        # Подписи модели — не код ни в strict, ни в relaxed (структурные
        # минимумы проверяются уже по очищенному кандидату).
        candidate = strip_prose_heading_lines(candidate, ext).strip()
        if not candidate:
            continue
        if not relaxed and _looks_like_prose(candidate, ext):
            continue
        cand_lines = candidate.splitlines()
        if len(cand_lines) < 4:
            continue
        # Повторный «jmp begin» внутри кандидата — склейка двух каркасов
        # (болванка из CoT + программа) — бракуем и в relaxed (res#3987):
        # такая склейка не компилируется ни при каком прозо-допущении.
        # Строки begin:/Ends: — законная часть каркаса.
        # Прелюдия (org/section/данные) может стоять до анкера — первый
        # `jmp begin` после неё и ЕСТЬ анкер, дублем он не считается.
        first_body = 0
        while (first_body < len(cand_lines)
               and _ASM_PRELUDE_RE.match(cand_lines[first_body])):
            first_body += 1
        inner_jmp = sum(1 for j, l in enumerate(cand_lines)
                        if j > first_body and _JMP_BEGIN_RE.match(l))
        if inner_jmp:
            continue
        if not relaxed:
            cut_code, _ = _cut_mid_prose(candidate, ext)
            cut_code = strip_code_fence_lines(cut_code).strip()
            if not cut_code or _looks_like_prose(cut_code, ext) or len(cut_code.splitlines()) < 4:
                continue
            candidate = cut_code
        candidates.append((marker_seen, start, end, candidate))
    return candidates


def extract_asm_i86(text, relaxed=False):
    """Структурный экстрактор ассемблера i8086 (DL-каркас с jmp begin, MASM).

    Возвращает код или "" (нет каркасных маркеров — не наш случай).

    Модели в CoT упоминают каркас и цитируют «болванки» программ; простая
    привязка к ПЕРВОМУ вхождению тащила болванку + прозу + программу скопом
    (res#3987). Поэтому: перебираем ВСЕ анкеры (jmp begin — и скашивания
    каркаса вроде jmp start/start: под MASM, либо MASM-директивы), от
    каждого — кандидат до ближайшего концевика, валидируем, берём ПОСЛЕДНИЙ
    валидный (модели цитируют болванки раньше финального ответа).

    relaxed=True — строгие прозя-валидации (не-проза, мид-проза) пропускаются:
    аварийный тир «долгое рассуждение без фенсов», когда все строгие тиры
    дали пусто; структурные минимумы (концевик, ≥4 строк, один jmp begin)
    остаются, а вердикт определит DL. Вызывающий код применяет гейт
    has_language_marker.
    """
    if not text:
        return ""
    lines = normalize_dl_marker_glue(text).splitlines()
    candidates = _asm_candidates(lines, relaxed=relaxed, ext=".asm")
    if not candidates:
        return ""
    # Выбор: ПОСЛЕДНИЙ кандидат (модели пишут болванки/цитаты раньше финального
    # ответа, res#4160: программа дана дважды — берём чистый второй заход);
    # если есть кандидаты с ;$E — среди них берём последний.
    marked = [c for c in candidates if c[0]]
    return (marked or candidates)[-1][3]


def collapse_duplicated_asm_answer(text):
    """Склеенный ответ «черновик + финал» → только финальная программа.

    Веб-пул отдаёт размышления и финальный ответ БЕЗ разделителя
    («...jmp ends ;$Ejmp begin...»): в «Полном ответе модели» и в
    «Извлечённом коде» программа видна дважды. Если последняя валидная
    программа ТОЧНО повторяет более раннюю (модель вывела тот же код в
    рассуждении и в ответе), оставляем текст с начала последней программы.
    Нет точного дубля (черновик отличается, программы нет, проза) — текст
    возвращается как есть (после безопасной нормализации склейки ;$E).
    """
    if not text:
        return text
    normalized = normalize_dl_marker_glue(text)
    lines = normalized.splitlines()
    candidates = _asm_candidates(lines)
    marked = [c for c in candidates if c[0]] or candidates
    if len(marked) < 2:
        return normalized
    last = marked[-1]
    if any(c[3] == last[3] for c in marked[:-1]):
        return "\n".join(lines[last[1]:]).strip()
    return normalized


def _salvage_glued_mpa(cand_lines, ext=".mpc"):
    """Спасение склеенного много-main окна С-МПА: декларации до ПЕРВОГО main
    + весь фрагмент от ПОСЛЕДНЕГО main до конца окна.

    Окно от якоря-декларации, закрывшееся на балансе ДАЛЬНЕГО main (первый
    main оказался незакрытым — обрез/болванка), раньше браковалось ЦЕЛИКОМ,
    и декларации выкусывались: DL падал «Undefined variable». Возвращает
    спасённый текст или None (меньше 4 строк / несбалансирован / не ровно
    один main). main и скобки ищем по маскированному тексту: `main(` и `}`
    в комментарии — не код.
    """
    judged = _scan_comment_spans(cand_lines, ext).blanked if ext else cand_lines
    main_indices = [j for j, l in enumerate(judged) if _MPA_MAIN_RE.match(l)]
    if len(main_indices) < 2:
        return None
    salvaged = cand_lines[:main_indices[0]] + cand_lines[main_indices[-1]:]
    text = strip_code_fence_lines("\n".join(salvaged)).strip()
    text_lines = [l for l in text.splitlines() if l.strip()]
    if len(text_lines) < 4:
        return None
    text_scan = _scan_comment_spans(text.splitlines(), ext) if ext else None
    code_view = "\n".join(text_scan.blanked) if text_scan else text
    if code_view.count("{") != code_view.count("}"):
        return None
    code_lines = text_scan.blanked if text_scan else text.splitlines()
    if sum(1 for l in code_lines if _MPA_MAIN_RE.match(l)) != 1:
        return None
    return text


def _closing_brace_line(line):
    """Строка, ЗАКРЫВАЮЩАЯ блочный кандидат: «}», «};» / «}» в хвосте строки.

    Скобка посреди строки (текст «{…}» в прозе) закрывающей не считается —
    окно должно кончаться на строке-скобке, как в настоящем коде.
    """
    stripped = (line.strip() or " ")
    return bool(_CLOSING_BRACE_RE.match(stripped) or _re.search(r"}\s*;?\s*$", stripped))


def _brace_depths(judged_lines):
    """Глубина фигурных скобок НА НАЧАЛЕ каждой строки (0 — файловый уровень).

    ``judged_lines`` — текст с уже замаскированными комментариями
    (``_scan_comment_spans(...).blanked``), иначе скобка в комментарии
    сдвинула бы глубину.
    """
    depth = 0
    depths = []
    for line in judged_lines:
        depths.append(depth)
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
    return depths


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
    scan = _scan_comment_spans(lines, ".mpc")
    judged = scan.blanked
    # Якоря — только на ФАЙЛОВОМ уровне (глубина скобок 0): локальные
    # объявления внутри функций (`int __bits(8) i;`) якорями не являются.
    # Иначе окно стартовало с середины функции, и в код уходил хвост fact()
    # без портов и заголовка — «Undefined variable x/R» в DL (кейс «Реши
    # задачу», C-МПА: `int fact(int n) { int __bits(16) f; int __bits(8) i; …`).
    depths = _brace_depths(judged)
    anchors = [
        i for i, line in enumerate(judged)
        if depths[i] == 0 and (_MPA_DECL_RE.match(line) or _MPA_MAIN_RE.match(line))
    ]
    # Запасные якоря — объявления ВНУТРИ функций: сами по себе они кандидаты
    # не годятся (обрезок с середины функции), но спасают «простыню» с
    # незакрытой болванкой выше финальной программы: та сдвигает программу на
    # глубину >0, окон файлового уровня там нет, и без запасных якорей код не
    # извлекался бы вовсе. Такие кандидаты — ТОЛЬКО если ни один файловый
    # якорь не дал результата (см. rank при выборе).
    anchor_set = set(anchors)
    inner_anchors = [
        i for i, line in enumerate(judged)
        if i not in anchor_set
        and (_MPA_DECL_RE.match(line) or _MPA_MAIN_RE.match(line))
    ]
    if not anchors and not inner_anchors:
        return ""

    def window_end(start):
        """Первое БАЛАНСНОЕ закрытие скобок после анкера; None — не сошлось.

        Считаем depth по всем «{»/«}» строк; конец — строка-скобка
        (_closing_brace_line), на которой depth вернулся в 0 ПОСЛЕ того,
        как был > 0 (окно обязано что-то открыть). Скобки в комментариях
        маскированы: `/* } */` не закрывает окно.
        """
        depth = 0
        opened = False
        for i in range(start, len(lines)):
            stripped = judged[i].strip() or " "
            depth += stripped.count("{") - stripped.count("}")
            if depth > 0:
                opened = True
            if opened and depth <= 0 and _closing_brace_line(judged[i]):
                return i
        return None

    def window_end_from_decl(start):
        """Конец программы от якоря-объявления: declи + хелперы + main.

        Прежний window_end от якоря-объявления закрывался на ПЕРВОЙ
        сбалансированной функции: если модель оформила хелперы (abs_val/sign)
        ДО main, в кандидата попадали только declи с первым хелпером, main
        выпадал — DL падал («Undefined variable …»). Здесь окончание окна —
        закрытие блока main; хелперы ДО main считаются частью программы.
        Если баланс до конца текста НЕ сошёлся (обрез/склейка) — окно до EOF:
        кандидат дальше может спасти мульти-main сальвадж «декларации +
        последний main», иначе отбракуется проверками ниже (именно так
        раньше пропадали сами декларации — Undefined variable).
        """
        depth = 0
        opened = False
        saw_main = False
        pending_end = None
        for i in range(start, len(lines)):
            stripped = judged[i].strip() or " "
            depth += stripped.count("{") - stripped.count("}")
            if depth > 0:
                opened = True
            if _MPA_MAIN_RE.match(judged[i]):
                saw_main = True
            if opened and depth <= 0 and _closing_brace_line(judged[i]):
                pending_end = i
                if saw_main:
                    return i
        if pending_end is not None:
            return pending_end
        # Баланс не сошёлся вовсе: тянуть до EOF имеет смысл только если в
        # окне был main (обрез/склейка, которую может спасти сальвадж);
        # без main это просто хвост текста — окна нет.
        return len(lines) - 1 if saw_main else None

    def end_for(start):
        """Якорь-объявление → до закрытия main (иначе теряются хелперы перед
        main); main-якорь → тело main, как раньше."""
        if _MPA_DECL_RE.match(judged[start]):
            return window_end_from_decl(start)
        return window_end(start)

    def window_start(anchor):
        """Подъём начала окна к блоку простых глобальных объявлений.

        Якорь-объявление ``__out``/``__bits`` не видит глобальные переменные
        БЕЗ портов (``char A[100];`` — входной массив, ``int x;``): окно
        начиналось с якоря, и объявления выше выпадали из кода («Undefined
        variable A» в DL; кейс «Реши задачу» C-МПА). Тянем начало вверх по
        подряд идущим простым объявлениям, пропуская пустые строки и
        строки-оградки; первая строка не-объявления — граница (проза/скобка).
        """
        i = anchor - 1
        while i >= 0:
            stripped = judged[i].strip()
            if not stripped or _FENCE_LINE_RE.match(judged[i]):
                i -= 1
                continue
            if _PLAIN_DECL_LINE_RE.match(judged[i]) and not _MPA_DECL_RE.match(judged[i]):
                anchor = i
                i -= 1
                continue
            break
        return anchor

    candidates = []
    for start in anchors + inner_anchors:
        rank = 0 if start in anchor_set else 1
        end = end_for(start)
        if end is None or end <= start:
            continue
        start = window_start(start)
        body = [l for l in lines[start:end + 1]]
        body = _strip_prose_tail(body, ext=".mpc")
        candidate = strip_code_fence_lines("\n".join(body)).strip()
        if not candidate:
            continue
        # Подписи модели — не код ни в strict, ни в relaxed: снимаем их ДО
        # проверок окон (main/баланс/дубли __bits/≥4 строк), чтобы валидация
        # видела финальную форму программы.
        candidate = strip_prose_heading_lines(candidate, ".mpc").strip()
        if not candidate:
            continue
        cand_line_count = len(candidate.splitlines())
        if cand_line_count < 4:
            continue
        cand_lines = candidate.splitlines()
        cand_scan = _scan_comment_spans(cand_lines, ".mpc")
        # Два main-а в окне — склейка двух болванок/программ: спасаем
        # «декларации + последний main» (иначе декларации выкусываются и DL
        # падает «Undefined variable»); не сошлось — в строгом режиме бракуем,
        # в relaxed отрезаем окно до ПОСЛЕДНЕГО main. Проверка ДО
        # дисбаланса-гейта: окно склейки обычно НЕсбалансировано (первый main
        # оборван) — иначе оно отбрасывалось бы раньше сальваджа и декларации
        # пропадали бы.
        main_indices = [
            j for j, l in enumerate(cand_scan.blanked) if _MPA_MAIN_RE.match(l)
        ]
        if len(main_indices) > 1:
            salvaged = _salvage_glued_mpa(cand_lines, ".mpc")
            if salvaged:
                candidate = salvaged
                cand_line_count = len(candidate.splitlines())
                cand_lines = candidate.splitlines()
                cand_scan = _scan_comment_spans(cand_lines, ".mpc")
            elif not relaxed:
                continue
            else:
                truncated = "\n".join(cand_lines[main_indices[-1]:])
                truncated = strip_code_fence_lines(truncated).strip()
                t_lines = truncated.splitlines()
                t_scan = _scan_comment_spans(t_lines, ".mpc")
                t_view = "\n".join(t_scan.blanked)
                if (
                    t_lines
                    and len(t_lines) >= 4
                    and t_view.count("{") <= t_view.count("}")
                ):
                    candidate = truncated
                    cand_line_count = len(t_lines)
                    cand_lines = t_lines
                    cand_scan = t_scan
                else:
                    continue
        # Программа не закрыта и не спасена — модель/болванка обрыв.
        cand_view = "\n".join(cand_scan.blanked)
        if cand_view.count("{") > cand_view.count("}"):
            continue
        if not relaxed:
            if _looks_like_prose(candidate, ".mpc"):
                continue
            # Дубли С-МПА-деклараций (одна и та же __bits-переменная объявлена
            # дважды) — склеились объявления из болванки и программы (в
            # пределах ОДНОГО окна; между окнами дубли — норма). Считаются
            # ТОЛЬКО объявления файлового уровня (глубина 0): одноимённые
            # ЛОКАЛЬНЫЕ переменные в разных функциях (`int __bits(16) f;` в
            # fact и в main) — нормальный C, а не склейка; раньше они
            # браковали полный кандидат, и в код уходил обрезок с середины
            # функции.
            mpa_decl_names = []
            cand_depths = _brace_depths(cand_scan.blanked)
            for j, line in enumerate(cand_scan.blanked):
                if cand_depths[j] == 0:
                    mpa_decl_names.extend(_MPA_DECL_NAME_RE.findall(line))
            mpa_decl_norm = [_re.sub(r"\s+", " ", d).strip().lower() for d in mpa_decl_names]
            if len(mpa_decl_norm) != len(set(mpa_decl_norm)):
                continue
            cut_code, _ = _cut_mid_prose(candidate, ".mpc")
            cut_code = strip_code_fence_lines(cut_code).strip()
            if not cut_code or _looks_like_prose(cut_code, ".mpc"):
                continue
            if not _looks_like_code(cut_code, ".mpc") and len(cut_code.splitlines()) < 4:
                continue
            candidate = cut_code
            cand_line_count = len(candidate.splitlines())
        candidates.append((rank, cand_line_count, candidate))
    if not candidates:
        return ""
    # Файловые якоря (rank 0) важнее запасных (rank 1): обрезок с середины
    # функции не должен перебить полную программу, даже если он длиннее.
    # Внутри ранга — больше строк (полный кандидат decls+main длиннее
    # main-одного); сортировка устойчива — при равенстве берётся ПОСЛЕДНИЙ.
    best_rank = min(rank for rank, _count, _cand in candidates)
    best = [item for item in candidates if item[0] == best_rank]
    best.sort(key=lambda t: t[1])
    return best[-1][2]


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
    """Быстрый тест: похоже ли на ассемблер i8086 (jmp begin / begin: / ends /
    MASM-скелет .model/.stack/.code/.data/org 100h)."""
    if not text:
        return False
    return bool(
        _JMP_BEGIN_RE.search(text)
        or _re.search(r"^\s*begin\s*:", text, _re.MULTILINE | _re.IGNORECASE)
        or _re.search(r"^\s*ends\s*:", text, _re.MULTILINE | _re.IGNORECASE)
        or _MASM_DIR_RE.search(text)
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


def language_code_incomplete(code, ext):
    """True — код выглядит НЕПОЛНЫМ (обрыв генерации посреди программы).

    Такой код не компилируется ни при каком допущении (нет концевика каркаса
    у .i86 / несбалансированные скобки или нет main у .mpc) — в DL его не
    отправляют, пара получает авто-ретрай «сразу код». Для прочих
    расширений полнота не проверяется (False).
    """
    if not code:
        return True
    ext = (ext or "").strip().lower()
    if ext in (".i86", ".asm"):
        if _DL_END_MARKER_RE.search(code):
            return False
        view = "\n".join(_scan_comment_spans(code.splitlines(), ext).blanked)
        return not bool(
            _re.search(
                r"^\s*(?:ends\s*:|jmp\s+ends\b|end\b)",
                view, _re.IGNORECASE | _re.MULTILINE,
            )
        )
    if ext == ".mpc":
        lines = code.splitlines()
        view = "\n".join(_scan_comment_spans(lines, ".mpc").blanked)
        if view.count("{") != view.count("}"):
            return True
        return not any(_MPA_MAIN_RE.match(line) for line in view.splitlines())
    return False


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


# --- Оградки: единственный владелец разбора --------------------------------
# Открывающая строка может нести любой «язык» (c-mpa, c++, c#…) и \r\n —
# прежний паттерн ([a-zA-Z]*\n) пропускал такие теги, и слово «c-mpa» попадало
# в извлечённый код первой строкой (ошибка компиляции на DL).
FENCED_BLOCK_RE = _re.compile(r"```[^\n]*\r?\n(.*?)```", _re.DOTALL)
# Незакрытая оградка (модель забывает закрывающие ``` — чаще при обрезке ответа):
# всё после открывающей строки трактуется как код до конца текста.
# Открывающая ``` может быть ПРИКЛЕЕНА к прозе («…Вывести только код.```asm\n»)
# — шаблон ищет её в любом месте строки, поэтому склейка не прячет финальный
# блок (логи GLM 5.3 Flash, живой прогон 2026-10-28).
UNCLOSED_FENCE_RE = _re.compile(r"```[^\n]*\r?\n(.*)\Z", _re.DOTALL)

# Кириллица в коде курса (.i86/.asm/.mpc). После маски комментариев/строк её
# наличие — протёкшая проза модели, а не код (реальные DL-ошибки
# «Неверный символ — russian letter»).
_CYRILLIC_RE = _re.compile(r"[А-Яа-яЁё]")

# Признак связного текста в строке «код + проза»: тире/многоточие. В коде
# курса (ассемблер/С-МПА) эти знаки вне комментариев/строк не встречаются,
# а `?` брать нельзя — в C-МПА `a ? b : c` это настоящий тернарник.
_INLINE_PROSE_RE = _re.compile(r"[—–…]")


def cut_prose_lines(text, ext=""):
    """Вырезать из фенс-блока строки с протёкшей прозой модели.

    В отличие от ``_mid_prose_lines`` (только бракует кандидата) здесь строка
    режется: внутри оградки модель уже отделила код, поэтому строка-рассуждение
    — мусор, а не повод выбросить всю программу. Строки, целиком лежащие в
    комментариях/строковых литералах, и защищённые метки не трогаются.

    Признаки прозы считаются по ``_scan_comment_spans`` (комментарий/строковый
    литерал замаскирован): кириллица вне комментариев/строк (код курса
    латинский) либо «код + связный текст» (≥3 словарных слова с тире/
    многоточием). Для прочих расширений правило не применяется — там свои
    синтаксисы.
    """
    if not text:
        return ""
    ext = (ext or "").strip().lower()
    if ext not in (".i86", ".asm", ".mpc"):
        return text
    lines = text.splitlines()
    scan = _scan_comment_spans(lines, ext)
    protected = _candidate_protected_labels(lines, ext)
    kept = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or i in scan.span_lines:
            kept.append(line)
            continue
        judged = scan.blanked[i]
        if _is_prose_heading_line(stripped, protected):
            continue
        if _CYRILLIC_RE.search(judged):
            continue
        words = _PROSE_WORD_RE.findall(judged)
        if len(words) >= 3 and _INLINE_PROSE_RE.search(judged):
            continue
        kept.append(line)
    return "\n".join(kept)


def sanitize_fenced_block(block, ext=""):
    """Привести содержимое одной ```-оградки к чистому коду.

    Оградка — контракт ответа: модель сама отделила код. Поэтому прозу внутри
    не «бракуем» (иначе теряем хорошую программу), а вырезаем: подписи
    (``strip_prose_heading_lines``), проза-строки посреди (``_cut_mid_prose``)
    и строки с протёкшим CoT (``cut_prose_lines``). ``""`` — кода не осталось.
    """
    code = strip_prose_heading_lines(block or "", ext)
    code, _ = _cut_mid_prose(code, ext)
    code = cut_prose_lines(code, ext)
    code = normalize_dl_marker_glue(code)
    code = strip_code_fence_lines(code)
    return code.strip()
