(function () {
    "use strict";

    const form = document.getElementById("check-ai-batch-form");
    const results = document.getElementById("check-ai-batch-results");
    if (form) setupUpload();
    if (results?.dataset.poll) setTimeout(refreshResults, 2500);

    function setupUpload() {
        const files = new Map();
        const extensions = new Set(form.dataset.extensions.split(","));
        const ignoredFolders = new Set(form.dataset.ignoredFolders.split(","));
        const maxFiles = Number(form.dataset.maxFiles);
        const maxBatchMib = Number(form.dataset.maxBatchMib);
        const selection = document.getElementById("check-ai-selection");
        const error = document.getElementById("check-ai-batch-error");
        const clear = document.getElementById("check-ai-clear");
        const submit = form.querySelector("[type=submit]");
        const inputs = [document.getElementById("check-ai-folders"), document.getElementById("check-ai-files")];
        let skipped = 0;

        function showError(message) {
            error.textContent = message;
            error.hidden = !message;
        }

        function updateSelection() {
            selection.textContent = files.size || skipped
                ? "Выбрано программ: " + files.size + ". Пропущено файлов: " + skipped + "."
                : "Исходники не выбраны.";
            submit.disabled = files.size === 0;
            clear.disabled = files.size === 0 && skipped === 0;
        }

        for (const input of inputs) {
            input.addEventListener("change", () => {
                for (const file of input.files) {
                    const path = file.webkitRelativePath || file.name;
                    const parts = path.split("/");
                    const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
                    const ignored = parts.some(part => part.startsWith("."))
                        || parts.slice(0, -1).some(part => ignoredFolders.has(part.toLowerCase()));
                    if (ignored || !extensions.has(suffix) || files.has(path)) {
                        skipped++;
                    } else {
                        files.set(path, file);
                    }
                }
                // Можно повторить выбор и добавить следующую папку в тот же пакет.
                input.value = "";
                showError("");
                updateSelection();
            });
        }

        clear.addEventListener("click", () => {
            files.clear();
            skipped = 0;
            showError("");
            updateSelection();
        });

        form.addEventListener("submit", async event => {
            event.preventDefault();
            if (!files.size) return;
            if (files.size > maxFiles) {
                showError("В одном пакете может быть до " + maxFiles + " файлов.");
                return;
            }

            let size = 0;
            const data = new FormData(form);
            data.set("skipped_files", skipped);
            for (const [path, file] of files) {
                size += file.size;
                data.append("files", file, file.name);
                data.append("paths", path);
            }
            if (size > maxBatchMib * 1024 * 1024) {
                showError("Общий размер программ в пакете — до " + maxBatchMib + " МиБ.");
                return;
            }

            showError("");
            selection.textContent = "Загрузка программ…";
            submit.disabled = clear.disabled = true;
            for (const input of inputs) input.disabled = true;
            try {
                const response = await fetch(form.action, {
                    method: "POST",
                    body: data,
                    headers: { Accept: "application/json" },
                });
                if (response.redirected) {
                    showError("Сеанс завершился. Войдите снова и повторите отправку.");
                    return;
                }
                const payload = await response.json().catch(() => ({}));
                if (!response.ok || !payload.url) {
                    showError(payload.error || "Не удалось загрузить пакет. Проверьте размер и повторите попытку.");
                    return;
                }
                window.location.replace(payload.url);
            } catch {
                showError("Не удалось связаться с сервером. Повторите отправку.");
            } finally {
                for (const input of inputs) input.disabled = false;
                updateSelection();
            }
        });
    }

    let lastResultHtml;
    async function refreshResults() {
        try {
            const url = new URL(window.location.href);
            url.searchParams.set("fragment", "1");
            const response = await fetch(url);
            if (response.redirected || [401, 403, 404].includes(response.status)) {
                results.querySelector('[role="status"]').textContent =
                    "Не удалось обновить результаты. Откройте страницу заново.";
                return;
            }
            if (!response.ok) throw new Error();

            const html = await response.text();
            if (html !== lastResultHtml) {
                // Обновление прогресса не сворачивает папки, которые открыл пользователь.
                const open = new Set(Array.from(results.querySelectorAll("details[open]"), el => el.dataset.path));
                const focusedPath = document.activeElement.closest("details")?.dataset.path;
                results.innerHTML = html;
                for (const details of results.querySelectorAll("details")) {
                    if (open.has(details.dataset.path)) details.open = true;
                    if (details.dataset.path === focusedPath) details.querySelector("summary").focus();
                }
                lastResultHtml = html;
            }
            if (results.querySelector('[data-done="true"]')) return;
        } catch {
            // Временный сбой соединения не влияет на очередь на сервере.
        }
        setTimeout(refreshResults, 2500);
    }
})();
