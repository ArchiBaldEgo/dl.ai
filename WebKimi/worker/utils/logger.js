const fs = require('fs');
const path = require('path');

const LOG_FILE = path.join(__dirname, '../logs/application.log');

// Создание директории для логов — один раз (lazy), не на каждый вызов лога.
let _logDirReady = false;
function ensureLogDirectory() {
    if (_logDirReady) return;
    const logDir = path.dirname(LOG_FILE);
    if (!fs.existsSync(logDir)) {
        fs.mkdirSync(logDir, { recursive: true });
    }
    _logDirReady = true;
}

// Основная функция логирования
function log(...messages) {
    ensureLogDirectory();

    const timestamp = new Date().toISOString();
    const message = messages.map(m =>
        typeof m === 'object' ? JSON.stringify(m, null, 2) : m
    ).join(' ');

    const logMessage = `[${timestamp}] ${message}\n`;

    // Вывод в консоль
    console.log(message);

    // Запись в файл (async — не блокирует event loop запросов бот-пула)
    fs.appendFile(LOG_FILE, logMessage, () => {});
}

// Логирование ошибок
function error(...messages) {
    ensureLogDirectory();

    const timestamp = new Date().toISOString();
    const message = `ERROR: ${messages.join(' ')}`;
    const logMessage = `[${timestamp}] ${message}\n`;

    console.error(logMessage.trim());
    fs.appendFile(LOG_FILE, logMessage, () => {});
}

module.exports = {
    log,
    error
}