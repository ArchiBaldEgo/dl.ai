(function () {
    "use strict";

    const protocol = document.getElementById("check-ai-protocol");
    if (!protocol) return;

    protocol.addEventListener("click", async event => {
        const button = event.target.closest("button[data-programs-url]");
        if (!button) return;

        const row = document.getElementById(button.getAttribute("aria-controls"));
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        button.textContent = expanded ? "Развернуть" : "Свернуть";
        row.hidden = expanded;
        if (expanded || row.getAttribute("aria-busy") === "true") return;

        const content = row.querySelector("[data-programs-content]");
        row.setAttribute("aria-busy", "true");
        content.textContent = "Загрузка программ…";
        try {
            const response = await fetch(button.dataset.programsUrl);
            if (!response.ok || response.redirected) throw new Error();
            content.innerHTML = await response.text();
        } catch {
            content.textContent = "Не удалось загрузить программы. Сверните пакет и откройте его ещё раз.";
        } finally {
            row.removeAttribute("aria-busy");
        }
    });
})();
