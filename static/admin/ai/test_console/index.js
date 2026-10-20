// Конфигурация страницы «Тестовая консоль» для static/admin/js/test_console.js.
// Вынесена из ai/templates/admin/ai/test_console.html.
// Файл сам по себе ничего не делает: он только собирает window.TEST_CONSOLE_CONFIG
// из json_script-блоков test-console-*-url, которые рендерит шаблон В extrahead
// (то есть ДО подключения test_console.js — тот читает конфиг в момент запуска).
(function () {
  'use strict';
  function readJsonScript(id, fallback) {
    var node = document.getElementById(id);
    if (!node) { return fallback; }
    try { var v = JSON.parse(node.textContent || 'null'); return v === null ? fallback : v; }
    catch (e) { return fallback; }
  }
  window.TEST_CONSOLE_CONFIG = {
    startUrl: readJsonScript('test-console-start-url', '/ai/admin/test-console/start/'),
    statusUrl: readJsonScript('test-console-status-url', '/ai/admin/test-console/status/'),
    logsUrl: readJsonScript('test-console-logs-url', '/ai/admin/test-console/logs/'),
    logViewUrl: readJsonScript('test-console-log-view-url', '/ai/admin/test-console/logs/view/')
  };
})();

