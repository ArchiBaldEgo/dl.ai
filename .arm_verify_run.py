"""Независимая перепроверка выкусывания: 25 тяжёлых задач x 5 моделей = 125.

Сверяем СОХРАНЁННЫЙ воркером code (не переизвлечение) с содержимым последнего
закрытого ```-блока: построчное равенство, без прозы/кириллицы/CoT/оградок.
"""

import os
import re
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "DjangoTest.settings")
django.setup()

import ai.arm_runner as ar  # noqa: E402
from ai.models import AIModelTestResult, Task, Topic  # noqa: E402
from ai.services import code_carver as cc  # noqa: E402

TASKS = {
    990201: ("пузырьковая сортировка + обмены",
             "Дан массив из 8 слов (знаковые). Отсортировать по возрастанию "
             "методом пузырька в том же массиве. Подсчитать количество обменов "
             "и записать в слово swaps."),
    990202: ("гласные и согласные",
             "Дана строка строчных латинских букв, оканчивающаяся '$'. "
             "Подсчитать количество гласных (a,e,i,o,u) в байт cnt_v и "
             "согласных в байт cnt_c."),
    990203: ("реверс строки",
             "Дана строка символов, оканчивающаяся '$'. Перевернуть её в том же "
             "буфере (символ '$' остаётся в конце)."),
    990204: ("максимум и его индекс",
             "Дан массив из 10 слов (знаковые). Найти максимальный элемент и его "
             "индекс: максимум в слово mx, индекс в слово idx."),
    990205: ("суммы положительных и отрицательных",
             "Дан массив из 12 байт (знаковые). Найти сумму положительных "
             "элементов в слово sum_pos и сумму отрицательных (по модулю) "
             "в слово sum_neg."),
    990206: ("сумма диагонали 3x3",
             "Дана матрица 3x3 слов. Найти сумму элементов главной диагонали, "
             "записать в слово s."),
    990207: ("двоичный поиск",
             "Дан отсортированный по возрастанию массив из 10 слов и слово key. "
             "Найти индекс key двоичным поиском; если элемента нет, записать -1. "
             "Индекс — слово."),
    990208: ("НОД Евклида",
             "Даны два слова a и b (беззнаковые). Найти их наибольший общий "
             "делитель алгоритмом Евклида, записать в слово gcd."),
    990209: ("проверка на простое число",
             "Дано слово n (2 <= n <= 30000). Записать в байт is_prime 1, если n "
             "простое, иначе 0."),
    990210: ("подсчёт вхождений символа",
             "Дана строка, оканчивающаяся '$', и байт ch. Подсчитать количество "
             "вхождений ch в строку, записать в байт cnt."),
    990211: ("шифр Цезаря +1",
             "Дана строка строчных латинских букв, оканчивающаяся '$'. Сдвинуть "
             "каждую букву на +1 в алфавите (z переходит в a), остальные символы "
             "не менять. Результат в том же буфере."),
    990212: ("удаление пробелов",
             "Дана строка, оканчивающаяся '$'. Удалить из неё все пробелы, "
             "сдвинув символы в том же буфере (символ '$' остаётся в конце)."),
    990213: ("слияние двух отсортированных массивов",
             "Даны массивы A и B по 4 слова, оба отсортированы по возрастанию. "
             "Записать слияние в массив C из 8 слов, сохранив порядок."),
    990214: ("сортировка вставками",
             "Дан массив из 8 байт (беззнаковые). Отсортировать по возрастанию "
             "методом вставок в том же массиве."),
    990215: ("сортировка выбором + сравнения",
             "Дан массив из 8 слов (знаковые). Отсортировать по возрастанию "
             "методом выбора в том же массиве. Подсчитать количество сравнений "
             "и записать в слово cmps."),
    990216: ("сумма десятичных цифр",
             "Дано слово n (0 <= n <= 65535). Найти сумму его десятичных цифр, "
             "записать в байт s."),
    990217: ("число в десятичную строку",
             "Дано слово n (0 <= n <= 65535). Записать его десятичное "
             "представление в буфер str, завершив символом '$'."),
    990218: ("Фибоначчи и сумма чётных",
             "Дано n (байт, 1 <= n <= 20). Записать первые n чисел Фибоначчи "
             "в массив fib (слова) и сумму чётных из них в слово s."),
    990219: ("скалярное произведение",
             "Даны массивы A и B по 6 слов (знаковые). Вычислить скалярное "
             "произведение, записать в слово dot."),
    990220: ("полином",
             "Даны слова a, b, c, x (знаковые). Вычислить y = a*x*x + b*x + c, "
             "записать в слово y."),
    990221: ("палиндром",
             "Дана строка, оканчивающаяся '$'. Записать в байт is_pal 1, если "
             "строка читается одинаково в обе стороны, иначе 0."),
    990222: ("вставка в отсортированный массив",
             "Дан отсортированный массив из 8 слов по возрастанию и слово x. "
             "Вставить x, сохранив порядок, в тот же массив (размер массива "
             "допускает 9 элементов). Записать новую длину в слово n."),
    990223: ("суммы строк матрицы 3x4",
             "Дана матрица 3x4 слов. Найти суммы элементов каждой строки и "
             "записать их в массив row_sum из 3 слов."),
    990224: ("количество слов в строке",
             "Дана строка, оканчивающаяся '$', слова разделены пробелами. "
             "Подсчитать количество слов, записать в байт cnt."),
    990225: ("замена символа в строке",
             "Дана строка, оканчивающаяся '$', байт from и байт to. Заменить все "
             "вхождения from на to в том же буфере."),
}

MODELS = [
    "Ollama_Glm_5_3_Flash_Cloud",
    "Ollama_DeepSeek_V4_1_Flash_Cloud",
    "Ollama_Kimi_K2_7_Code_Cloud",
    "Ollama_Gemma_4_Cloud",
    "Ollama_Gpt_Oss_120B_Cloud",
]

COT_WORDS = ("wait", "let me", "hmm", "i think", "actually", "проверю",
             "итоговый код", "final code", "let's", "сначала", "постой")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

topic = Topic.objects.get(pk=1)
node_ids = []
for node_id, (name, statement) in TASKS.items():
    Task.objects.update_or_create(
        node_id=node_id,
        defaults={
            "name": name,
            "statement": statement,
            "file_extension": ".i86",
            "task_id": node_id,
            "topic": topic,
        },
    )
    node_ids.append(node_id)

run_id, err = ar.start_batch_solve_run(
    node_ids,
    MODELS,
    user_id=1,
    session_id="local-live-no-dl",
    ui_language="Русский",
    dl_test=False,
    solve_file_extension=".i86",
    solve_prog_lang_name="Ассемблер i8086",
    record_stats=False,
    save_solutions=False,
    run_name="verification extraction check",
)
print("run_id:", run_id, "err:", err, "pairs:", len(node_ids) * len(MODELS), flush=True)
if not run_id:
    raise SystemExit(1)

deadline = time.time() + 5 * 3600
last = None
while time.time() < deadline:
    job = ar._jobs.get(run_id) or {}
    state = (job.get("status"), job.get("completed_pairs"), job.get("total_pairs"))
    if state != last:
        print("status=%s pairs=%s/%s" % state, flush=True)
        last = state
    if job.get("status") not in ("running", None):
        break
    time.sleep(5)


def norm(lines):
    return [l.strip() for l in lines if l.strip()]


rows = list(
    AIModelTestResult.objects.filter(run__run_id=run_id)
    .select_related("task")
    .order_by("model_key", "task__node_id")
)
print("\nresults:", len(rows), flush=True)

stats = {}
failures = []
no_end = {}
for row in rows:
    code = row.code or ""
    raw = row.raw_response or ""
    issues = []
    if not raw.strip():
        issues.append("model_error")
    if not code.strip():
        issues.append("empty")
    scan = cc._scan_comment_spans(code.splitlines(), ".i86") if code else None
    judged = "\n".join(scan.blanked) if scan else ""
    if re.search(r"[А-Яа-яЁё]", judged):
        issues.append("cyrillic")
    cot = [w for w in COT_WORDS if w in judged.lower()]
    if cot:
        issues.append("cot:" + ",".join(cot))
    if "```" in code:
        issues.append("fence_leak")
    if code and not cc.has_language_marker(code, ".i86"):
        issues.append("no_marker")
    if row.truncated:
        issues.append("truncated")

    fences = FENCE_RE.findall(raw)
    if fences and code.strip():
        expected = norm(cc.sanitize_fenced_block(fences[-1], ".i86").splitlines())
        got = norm(code.splitlines())
        if expected != got:
            missing = [l for l in expected if l not in got]
            extra = [l for l in got if l not in expected]
            issues.append("diff(missing=%d,extra=%d)" % (len(missing), len(extra)))
    # сохранённый код обязан совпадать с повторным извлечением
    if raw.strip() and norm(ar._extract_code_from_response(raw, ".i86").splitlines()) != norm(code.splitlines()):
        issues.append("stored_ne_reextract")

    if code and cc.language_code_incomplete(code, ".i86"):
        no_end.setdefault(row.model_key, []).append(row.task.node_id)

    st = stats.setdefault(row.model_key, {"total": 0, "clean": 0, "issues": {}})
    st["total"] += 1
    if not issues:
        st["clean"] += 1
    for i in issues:
        key = i.split(":")[0].split("(")[0]
        st["issues"][key] = st["issues"].get(key, 0) + 1
    if issues:
        failures.append((row, issues, code, raw))

print("\n" + "=" * 72, flush=True)
for model, st in sorted(stats.items()):
    print(f"{model}: clean {st['clean']}/{st['total']}  issues={st['issues']}", flush=True)
print("no END (model style):", {k: len(v) for k, v in no_end.items()}, flush=True)

print(f"\nfailures: {len(failures)}", flush=True)
for row, issues, code, raw in failures:
    print("\n" + "-" * 72, flush=True)
    print(f"model={row.model_key} task={row.task.node_id} ({row.task.name}) issues={issues}",
          flush=True)
    print("CODE TAIL:", repr(code[-800:]), flush=True)
    print("RAW TAIL:", repr(raw[-1000:]), flush=True)

print("\nrun_id_for_cleanup:", run_id, flush=True)
