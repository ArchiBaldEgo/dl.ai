// Пауза
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Генерация случайного числа в диапазоне
function random(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

// Проверка на таймаут ошибку
function isTimeoutError(error) {
    return error.message.includes('timeout') || error.message.includes('Timeout');
}

module.exports = {
    sleep,
    random,
    isTimeoutError
};