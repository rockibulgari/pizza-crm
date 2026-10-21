// Карточка заказа: подтверждение отмены и защита от двойного нажатия.
(function () {
  "use strict";

  const form = document.getElementById("status-form");
  let sending = false;

  form.addEventListener("submit", function (event) {
    // Второе нажатие, пока первое ещё отправляется, — иначе клиенту уйдут два сообщения
    if (sending) {
      event.preventDefault();
      return;
    }
    const button = event.submitter;
    if (button && button.hasAttribute("data-cancel") && !confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    sending = true;
    // Кнопки не выключаем (disabled): у выключенной кнопки браузер не отправит её значение
    form.classList.add("sending");
    if (button) button.classList.add("pressed");
  });

  // «Назад» в браузере может вернуть страницу из кэша — снова разрешаем нажатия
  window.addEventListener("pageshow", function () {
    sending = false;
    form.classList.remove("sending");
    form.querySelectorAll(".pressed").forEach(function (b) { b.classList.remove("pressed"); });
  });
})();
