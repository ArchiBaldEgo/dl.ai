// static/base/sections.js

(function () {
  function loadPagesEvent() {
    const sections = Array.from(document.querySelectorAll(".section"));
    const navContainer = document.getElementById("section_tabs");

    if (!sections.length || !navContainer) return;

    const buttons = new Map();

    // Функция переключения активной секции
    function activateSection(targetId) {
      sections.forEach((sec) => {
        const isMatch = sec.id === targetId;
        sec.style.display = isMatch ? "block" : "none";
      });

      buttons.forEach((btn, id) => {
        btn.classList.toggle("active", id === targetId);
      });

      // Сохраняем выбор в URL без перезагрузки страницы
      history.replaceState(null, "", `#${targetId}`);
    }

    // Генерируем кнопки на основе секций в DOM
    sections.forEach((sec, index) => {
      // Если забыли указать id в шаблоне, генерируем запасной
      if (!sec.id) sec.id = `section_${index}`;

      const titleEl = sec.querySelector(".section_name");
      const title = sec.dataset.title || (titleEl ? titleEl.textContent.trim() : `Раздел ${index + 1}`);

      const btn = document.createElement("button");
      btn.type = "button"; // Важно, чтобы кнопка не отправляла формы, если окажется внутри <form>
      btn.className = "section_tab";
      btn.textContent = title;

      btn.addEventListener("click", () => activateSection(sec.id));

      navContainer.appendChild(btn);
      buttons.set(sec.id, btn);
    });

    // При загрузке открываем секцию из URL (#id) или первую по умолчанию
    const initialHash = window.location.hash.replace("#", "");
    const defaultId = buttons.has(initialHash) ? initialHash : sections[0].id;
    activateSection(defaultId);
  }

  function loadPages() {
    document.addEventListener("DOMContentLoaded", () => loadPagesEvent());
  }

  function main() {
    loadPages()
  }
  main()
})();