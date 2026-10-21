// Логика страницы «Документация»: правка файла документации видна без
// перезагрузки страницы — каждые 10 с спрашиваем fingerprint главы;
// изменился — заменяем тело (HTML рендерится сервером из нашего markdown,
// пользовательский ввод в него не попадает). Сессия протухла / сеть
// недоступна — тихо прекращаем поллинг, а не пугаем пользователя ошибками.
// Вынесена из ai/templates/admin/ai/docs.html.
// Шаблонных переменных здесь нет: fingerprint и slug приходят через
// json_script-блоки (ai-docs-fingerprint / ai-docs-slug), которые рендерит
// шаблон ДО подключения этого файла.
(function () {
  var rawFp = document.getElementById('ai-docs-fingerprint');
  var rawSlug = document.getElementById('ai-docs-slug');
  var contentEl = document.querySelector('.ai-docs-content');
  if (!rawFp || !rawSlug || !contentEl) return;
  var fingerprint;
  var slug;
  try {
    fingerprint = JSON.parse(rawFp.textContent);
    slug = JSON.parse(rawSlug.textContent);
  } catch (e) { return; }
  if (!fingerprint || !slug) return;

  var timer = null;
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }
  function poll() {
    fetch('/ai/admin/docs/' + encodeURIComponent(slug) + '/content/', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.success || !data.html) throw new Error('bad payload');
        var fp = data.fingerprint || [];
        if (fp.length === 2 && (fp[0] !== fingerprint[0] || fp[1] !== fingerprint[1])) {
          fingerprint = fp;
          var scrollY = window.scrollY;
          contentEl.innerHTML = data.html;
          window.scrollTo(0, scrollY);
        }
      })
      .catch(function () { stop(); });
  }
  timer = setInterval(poll, 10000);
})();
