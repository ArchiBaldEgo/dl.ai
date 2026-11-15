// static/js/base.js
/*
    Файл который всегда подгружается базово на любой странице
*/
'use strict';

(function () {
  // Задаём Title для удобства страничек
  function setTitle() {
    const h1 = document.querySelector("#content h1");
    document.title = h1.firstChild.textContent.trim();
  }

  function main() {
    setTitle();
  }

  document.addEventListener("DOMContentLoaded", main);
})();