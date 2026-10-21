// Сотрудники: подтверждение перед отключением.
(function () {
  "use strict";

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
})();
