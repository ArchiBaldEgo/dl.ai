(function () {
    "use strict";

    const form = document.getElementById("check-ai-form");
    const textarea = form?.querySelector("#id_text");
    const userId = form?.dataset.userId;
    if (!textarea || !userId) return;

    // В одном браузере у каждой учётной записи своя высота поля.
    const key = `ai_admin:check_ai:textarea_height:${userId}`;
    const pixelHeight = /^\d+(?:\.\d+)?px$/;
    try {
        const savedHeight = localStorage.getItem(key);
        if (pixelHeight.test(savedHeight)) textarea.style.height = savedHeight;
    } catch {
        return; // Без доступа к хранилищу поле всё равно можно растянуть.
    }

    // При перетаскивании уголка браузер записывает высоту в style поля.
    const observer = new MutationObserver(() => {
        const height = textarea.style.height;
        if (!pixelHeight.test(height)) return;
        try {
            localStorage.setItem(key, height);
        } catch {
            // При сбое хранилища текущая высота сохраняется до ухода со страницы.
        }
    });
    observer.observe(textarea, { attributes: true, attributeFilter: ["style"] });
})();
