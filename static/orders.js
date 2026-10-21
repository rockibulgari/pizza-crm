// Список заказов: автопроверка раз в 15 секунд, звук и подсветка новых заказов.
(function () {
  "use strict";

  const REFRESH_MS = 15000;     // как часто спрашивать сервер
  const HIGHLIGHT_MS = 30000;   // сколько подсвечивать новые строки
  const SOUND_KEY = "pizza-crm-sound";

  const freshUrl = document.currentScript.dataset.freshUrl;
  const list = document.getElementById("orders-list");
  const offline = document.getElementById("offline");
  const soundButton = document.getElementById("sound-button");
  const pageTitle = document.title;

  const filter = document.getElementById("filter");
  const searchForm = document.querySelector(".search");
  const searchInput = document.getElementById("search-input");
  const searchClear = document.getElementById("search-clear");

  let lastId = readLastId();
  let unseen = 0;               // сколько новых заказов пришло, пока сотрудник не смотрел
  const fresh = new Map();      // номер заказа → до какого времени его подсвечивать

  function readLastId() {
    const data = list.querySelector(".list-data");
    return data ? Number(data.dataset.lastId) : 0;
  }

  // Числа в кнопке фильтра и в его списке — из свежего куска страницы
  function showCounts() {
    const data = list.querySelector(".list-data");
    if (!data) return;
    const counts = JSON.parse(data.dataset.counts);
    document.querySelectorAll("[data-count]").forEach(function (el) {
      el.textContent = counts[el.dataset.count];
    });
  }

  // ---------- Фильтр по статусу ----------
  // Открывается и закрывается сам (<details>); здесь — закрытие по клику
  // мимо панели (на затемнение) и по Esc.

  document.getElementById("filter-backdrop").addEventListener("click", function () {
    filter.open = false;
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && filter.open) {
      filter.open = false;
      filter.querySelector("summary").focus();
    }
  });

  // ---------- Поиск ----------
  // Ищем по Enter (обычная отправка формы). Крестик виден, когда в поле есть текст.

  function showClear() {
    searchClear.hidden = searchInput.value === "";
  }

  searchInput.addEventListener("input", showClear);
  searchClear.addEventListener("click", function () {
    searchInput.value = "";
    showClear();
    if (searchForm.dataset.applied) {
      searchForm.submit();  // поиск уже был применён — показываем список без него
    } else {
      searchInput.focus();
    }
  });

  // ---------- Звук ----------
  // Браузер разрешает звук только после клика по странице, поэтому
  // AudioContext создаётся (или «будится») по клику.

  let soundOn = false;
  let audio = null;

  try {
    soundOn = localStorage.getItem(SOUND_KEY) === "on";
  } catch (e) { /* в приватном режиме localStorage может быть недоступен */ }

  function showSoundButton() {
    soundButton.textContent = soundOn ? "🔔" : "🔕";
    soundButton.setAttribute("aria-pressed", String(soundOn));
    soundButton.title = soundOn
      ? "Звук новых заказов включён — нажмите, чтобы выключить"
      : "Звук новых заказов выключен — нажмите, чтобы включить";
  }

  function wakeAudio() {
    if (!soundOn) return;
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === "suspended") audio.resume();
  }

  function beep() {
    if (!soundOn || !audio) return;
    // Два коротких тона: «динь-дон»
    [[880, 0], [1320, 0.18]].forEach(function ([freq, delay]) {
      const start = audio.currentTime + delay;
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.4, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.3);
      osc.connect(gain).connect(audio.destination);
      osc.start(start);
      osc.stop(start + 0.32);
    });
  }

  soundButton.addEventListener("click", function () {
    soundOn = !soundOn;
    try {
      localStorage.setItem(SOUND_KEY, soundOn ? "on" : "off");
    } catch (e) { /* не запомнится — не страшно */ }
    showSoundButton();
    wakeAudio();
    beep();  // сразу слышно, что звук работает
  });

  // Любой клик или нажатие клавиши «будит» звук после перезагрузки страницы
  document.addEventListener("pointerdown", wakeAudio);
  document.addEventListener("keydown", wakeAudio);

  // ---------- Заголовок вкладки ----------

  function showTitle() {
    document.title = unseen ? "(" + unseen + ") Новые заказы" : pageTitle;
  }

  function markSeen() {
    if (!unseen) return;
    unseen = 0;
    showTitle();
  }

  document.addEventListener("pointerdown", markSeen);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") markSeen();
  });

  // ---------- Подсветка новых строк ----------

  function highlight() {
    const now = Date.now();
    fresh.forEach(function (until, id) {
      if (until < now) fresh.delete(id);
    });
    list.querySelectorAll("tr[data-id]").forEach(function (row) {
      row.classList.toggle("fresh", fresh.has(Number(row.dataset.id)));
    });
  }

  // ---------- Автопроверка ----------

  async function refresh() {
    let response;
    try {
      response = await fetch(freshUrl + location.search, { cache: "no-store" });
    } catch (e) {
      offline.hidden = false;  // сеть пропала — попробуем через 15 секунд
      return;
    }
    // Смена кончилась или сотрудника отключили — сервер отправил на вход
    if (response.redirected) {
      location.reload();
      return;
    }
    if (!response.ok) {
      offline.hidden = false;
      return;
    }
    offline.hidden = true;

    list.innerHTML = await response.text();
    showCounts();
    const newLastId = readLastId();
    if (newLastId > lastId) {
      const until = Date.now() + HIGHLIGHT_MS;
      for (let id = lastId + 1; id <= newLastId; id++) fresh.set(id, until);
      unseen += newLastId - lastId;
      lastId = newLastId;
      showTitle();
      beep();
    }
    highlight();
  }

  setInterval(refresh, REFRESH_MS);
  setInterval(highlight, 5000);  // снять подсветку вовремя, не дожидаясь проверки

  // ---------- Клик по строке открывает заказ ----------

  list.addEventListener("click", function (event) {
    if (event.target.closest("a")) return;  // по ссылке браузер перейдёт сам
    const row = event.target.closest("tr[data-href]");
    if (row) location.href = row.dataset.href;
  });

  showSoundButton();
  showClear();  // при «Назад» браузер мог вернуть текст в поле
})();
