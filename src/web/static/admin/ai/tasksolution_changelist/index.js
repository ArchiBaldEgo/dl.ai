// Логика списка «Решённые задачи (кэш кода)»: клик по строке раскрывает
// сохранённый код, клик по ссылке на задачу — нет (она ведёт в DL).
// Вынесена из ai/templates/admin/ai/tasksolution_changelist.html.
// Шаблонных переменных здесь нет.
// Клик по строке раскрывает сохранённый код; клик по ссылке на задачу —
// нет (она ведёт в DL). Открыта одна деталь за раз (паттерн request_logs).
(function () {
  document.addEventListener('click', function (event) {
    if (event.target.closest('a')) return;
    var row = event.target.closest('.ai-sol-row');
    if (!row || !row.closest('#ai-sol-table')) return;
    var detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('ai-sol-detail-row')) return;
    var isOpen = detailRow.classList.contains('open');
    document.querySelectorAll('.ai-sol-detail-row.open').forEach(function (r) {
      r.classList.remove('open');
    });
    if (!isOpen) detailRow.classList.add('open');
  });
})();
