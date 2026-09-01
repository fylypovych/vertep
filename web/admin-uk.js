(() => {
  "use strict";

  const textReplacements = new Map([
    ["Jobs", "Завдання"],
    ["Workers", "Вузли"],
    ["Workflows", "Сценарії обробки"],
    ["Add Worker", "Додати вузол"],
    ["Додати Worker", "Додати вузол"],
    ["Core Node", "Головний вузол"],
    ["GPU Node", "GPU-вузол"],
    ["Text Node", "Текстовий вузол"],
    ["Voice Node", "Голосовий вузол"],
    ["Publisher Node", "Вузол публікації"],
    ["Backup Node", "Вузол резервного копіювання"],
    ["Monitoring Node", "Вузол моніторингу"],
    ["Dead-letter queue", "Черга помилкових завдань"],
    ["Alerts", "Сповіщення"],
    ["CORE ONLINE", "Ядро працює"],
    ["Node", "Вузол"],
    ["Capabilities", "Можливості"],
    ["Task", "Поточне завдання"],
    ["Update State", "Стан оновлення"],
    ["Self-test", "Самоперевірка"],
    ["Certificate", "Сертифікат"],
    ["Approve", "Схвалити"],
    ["Publish", "Опублікувати"],
    ["Pause", "Призупинити"],
    ["Resume", "Продовжити"],
    ["Retry", "Повторити"],
    ["Regenerate", "Згенерувати повторно"],
    ["Cancel", "Скасувати"],
    ["Delete", "Видалити"],
    ["Retry publish", "Повторити публікацію"],
    ["Drain", "Завершити поточні завдання"],
    ["Rotate", "Оновити сертифікат"],
    ["Revoke", "Відкликати доступ"],
    ["Quarantine", "Ізолювати"],
    ["VALID", "Справний"],
    ["INVALID", "Містить помилки"],
    ["CONFIGURED", "Налаштовано"],
    ["NOT CONFIGURED", "Не налаштовано"],
    ["Installer Command", "Команда встановлення"],
    ["Cleanup dry-run", "Перевірити очищення"],
    ["Zero-Shell lifecycle", "Обслуговування без консолі"],
    ["Backups", "Резервні копії"],
    ["Models", "Моделі"],
    ["Restore", "Відновити"],
    ["ONLINE", "У мережі"],
    ["BUSY", "Зайнятий"],
    ["FREE", "Вільний"],
    ["UPDATING", "Оновлюється"],
    ["OFFLINE", "Не в мережі"],
    ["ERROR", "Помилка"],
    ["QUARANTINED", "Ізольований"],
    ["DRAINING", "Завершує завдання"],
  ]);

  const phraseReplacements = [
    ["Усього Jobs", "Усього завдань"],
    ["Workers online", "Вузлів у мережі"],
    ["Workers відсутні", "Вузлів немає"],
    ["Workflows відсутні", "Сценаріїв обробки немає"],
    ["Новий workflow", "Новий сценарій обробки"],
    ["Створити Job", "Створити завдання"],
    ["Видалити прострочені Jobs", "Видалити прострочені завдання"],
    ["Registration Token", "токен реєстрації"],
    ["нового Worker", "нового вузла"],
    ["до Core", "до головного вузла"],
    ["на Core", "на головному вузлі"],
    ["installer command", "команду встановлення"],
    ["First Run Wizard", "майстер першого запуску"],
    ["Web Wizard", "вебмайстер"],
    ["non-Core вузлів", "додаткових вузлів"],
    ["Core URL", "адресу головного вузла"],
    ["Push-based", "Автоматичне"],
    ["capabilities", "можливості"],
    ["Backup/restore", "Резервне копіювання й відновлення"],
    ["AI models", "моделі ШІ"],
    ["TLS certificates", "TLS-сертифікати"],
    ["Створити backup", "Створити резервну копію"],
    ["Backup відсутні", "Резервних копій немає"],
    ["Models відсутні", "Моделей немає"],
    ["Ollama model", "моделі Ollama"],
    ["TLS certificate", "TLS-сертифікат"],
  ];

  const translateText = (value) => {
    const trimmed = value.trim();
    if (!trimmed) return value;
    let translated = textReplacements.get(trimmed) || trimmed;
    for (const [source, target] of phraseReplacements) {
      translated = translated.replaceAll(source, target);
    }
    if (translated === trimmed) return value;
    return value.replace(trimmed, translated);
  };

  const translateTree = (root) => {
    if (root.nodeType === Node.TEXT_NODE) {
      const translated = translateText(root.nodeValue || "");
      if (translated !== root.nodeValue) root.nodeValue = translated;
      return;
    }
    if (!(root instanceof Element)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const translated = translateText(node.nodeValue || "");
      if (translated !== node.nodeValue) node.nodeValue = translated;
    });
  };

  document.title = "Vertep — панель керування";
  const roleLabels = {
    core: "Головний вузол",
    gpu: "GPU-вузол",
    text: "Текстовий вузол",
    voice: "Голосовий вузол",
    publisher: "Вузол публікації",
    backup: "Вузол резервного копіювання",
    monitoring: "Вузол моніторингу",
  };
  document.querySelectorAll("#workerrole option, #filterRole option").forEach((option) => {
    if (roleLabels[option.value]) option.textContent = roleLabels[option.value];
  });
  translateTree(document.body);
  new MutationObserver((records) => {
    for (const record of records) {
      record.addedNodes.forEach(translateTree);
      if (record.type === "characterData") translateTree(record.target);
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });

  const dialog = document.querySelector("#chardialog");
  const rawEditor = document.querySelector("#charjson");
  if (!dialog || !rawEditor) return;

  dialog.classList.add("character-dialog");
  rawEditor.hidden = true;
  dialog.querySelectorAll(":scope > button").forEach((button) => button.remove());
  dialog.querySelector("h2").textContent = "Персонаж";
  dialog.querySelector("h2").insertAdjacentHTML(
    "afterend",
    `<form id="characterform" class="character-form">
      <div class="field">
        <label for="character-name">Ім’я персонажа</label>
        <input id="character-name" maxlength="120" required placeholder="Наприклад, Дід Самогонщик">
      </div>
      <div class="field">
        <label for="character-id">Системний ідентифікатор</label>
        <input id="character-id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]{1,63}" required placeholder="did_samogon">
        <small class="hint">Латинські малі літери, цифри, дефіс або підкреслення.</small>
      </div>
      <div class="field">
        <label for="character-language">Мова</label>
        <select id="character-language">
          <option value="uk">Українська</option>
          <option value="en">Англійська</option>
          <option value="pl">Польська</option>
          <option value="de">Німецька</option>
          <option value="other">Інша</option>
        </select>
      </div>
      <label class="toggle"><input id="character-enabled" type="checkbox"> Персонаж активний</label>
      <div class="field wide">
        <label for="character-prompt">Опис характеру та поведінки</label>
        <textarea id="character-prompt" placeholder="Опишіть стиль мовлення, характер, знання та обмеження персонажа."></textarea>
        <small class="hint">Цей текст використовується як системна інструкція для моделі.</small>
      </div>
      <fieldset>
        <legend>Зовнішність</legend>
        <label for="character-style">Візуальний стиль</label>
        <textarea id="character-style" placeholder="Наприклад, тепла документальна ілюстрація"></textarea>
        <label for="character-ratio">Формат кадру</label>
        <select id="character-ratio">
          <option value="16:9">16:9 — горизонтальний</option>
          <option value="9:16">9:16 — вертикальний</option>
          <option value="1:1">1:1 — квадратний</option>
          <option value="4:3">4:3 — класичний</option>
        </select>
      </fieldset>
      <fieldset>
        <legend>Голос</legend>
        <label for="character-voice-provider">Постачальник голосу</label>
        <select id="character-voice-provider">
          <option value="none">Без озвучення</option>
          <option value="local">Локальний</option>
          <option value="external">Зовнішній сервіс</option>
        </select>
        <label for="character-voice">Назва або ідентифікатор голосу</label>
        <input id="character-voice" placeholder="Необов’язково">
      </fieldset>
      <fieldset>
        <legend>Генерація</legend>
        <label for="character-workflow">Сценарій обробки</label>
        <input id="character-workflow" placeholder="workflows/image/demo.json">
        <label for="character-vram">Мінімум відеопам’яті, МБ</label>
        <input id="character-vram" type="number" min="0" step="1">
        <label for="character-retries">Максимум повторних спроб</label>
        <input id="character-retries" type="number" min="0" max="20" step="1">
      </fieldset>
      <fieldset>
        <legend>Публікація</legend>
        <label class="toggle"><input id="character-publishing" type="checkbox"> Дозволити автоматичну публікацію</label>
        <small class="hint">Канали публікації налаштовуються окремо в захищених інтеграціях.</small>
      </fieldset>
      <p id="character-error" class="form-error wide" role="alert"></p>
    </form>
    <div class="dialog-actions">
      <button id="character-save" type="button">Зберегти</button>
      <button id="character-close" type="button" class="secondary">Скасувати</button>
    </div>`,
  );

  let currentCharacter = null;
  const field = (id) => document.querySelector(`#${id}`);
  const clone = (value) => JSON.parse(JSON.stringify(value || {}));
  const errorBox = field("character-error");

  const showError = (message = "") => {
    errorBox.textContent = message;
    errorBox.classList.toggle("visible", Boolean(message));
  };

  const populateCharacterForm = (character, isNew) => {
    currentCharacter = clone(character);
    field("character-name").value = character.name || "";
    field("character-id").value = character.id || "";
    field("character-id").disabled = !isNew;
    const language = ["uk", "en", "pl", "de"].includes(character.language)
      ? character.language
      : "other";
    field("character-language").value = language;
    field("character-language").dataset.original = character.language || "uk";
    field("character-enabled").checked = character.enabled !== false;
    field("character-prompt").value = character.system_prompt || "";
    field("character-style").value = character.visual?.style || "";
    field("character-ratio").value = character.visual?.aspect_ratio || "16:9";
    field("character-voice-provider").value = character.voice?.provider || "none";
    field("character-voice").value = character.voice?.voice || "";
    field("character-workflow").value =
      character.generation?.workflow || character.workflow || "workflows/image/demo.json";
    field("character-vram").value = character.generation?.min_vram_mb ?? 4096;
    field("character-retries").value = character.generation?.max_retries ?? 3;
    field("character-publishing").checked = character.publishing?.enabled === true;
    dialog.querySelector("h2").textContent = isNew ? "Новий персонаж" : "Редагування персонажа";
    showError();
    dialog.showModal();
    field("character-name").focus();
  };

  window.newCharacter = () =>
    populateCharacterForm(
      {
        id: "new_character",
        name: "Новий персонаж",
        language: "uk",
        enabled: true,
        system_prompt: "",
        voice: { provider: "none", language: "uk", voice: null },
        visual: { style: "", aspect_ratio: "16:9" },
        generation: {
          workflow: "workflows/image/demo.json",
          min_vram_mb: 4096,
          max_retries: 3,
        },
        publishing: { enabled: false, channels: [] },
      },
      true,
    );

  window.editCharacter = async (id) => {
    try {
      const character = await window.api(`/api/characters/${encodeURIComponent(id)}`);
      populateCharacterForm(character, false);
    } catch (error) {
      window.alert(`Не вдалося завантажити персонажа: ${error.message}`);
    }
  };

  window.saveCharacter = async () => {
    const form = field("characterform");
    if (!form.reportValidity()) return;
    const id = field("character-id").value.trim();
    if (!/^[a-z0-9][a-z0-9_-]{1,63}$/.test(id)) {
      showError("Системний ідентифікатор має містити лише малі латинські літери, цифри, дефіс або підкреслення.");
      field("character-id").focus();
      return;
    }
    const languageSelect = field("character-language");
    const language =
      languageSelect.value === "other" ? languageSelect.dataset.original || "uk" : languageSelect.value;
    const character = {
      ...clone(currentCharacter),
      id,
      name: field("character-name").value.trim(),
      language,
      enabled: field("character-enabled").checked,
      system_prompt: field("character-prompt").value.trim(),
      workflow: field("character-workflow").value.trim() || null,
      voice: {
        ...clone(currentCharacter?.voice),
        provider: field("character-voice-provider").value,
        language,
        voice: field("character-voice").value.trim() || null,
      },
      visual: {
        ...clone(currentCharacter?.visual),
        style: field("character-style").value.trim(),
        aspect_ratio: field("character-ratio").value,
      },
      generation: {
        ...clone(currentCharacter?.generation),
        workflow: field("character-workflow").value.trim() || "workflows/image/demo.json",
        min_vram_mb: Number(field("character-vram").value || 0),
        max_retries: Number(field("character-retries").value || 0),
      },
      publishing: {
        ...clone(currentCharacter?.publishing),
        enabled: field("character-publishing").checked,
        channels: currentCharacter?.publishing?.channels || [],
      },
    };
    const saveButton = field("character-save");
    saveButton.disabled = true;
    saveButton.textContent = "Збереження…";
    showError();
    try {
      await window.api(`/api/characters/${encodeURIComponent(id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(character),
      });
      dialog.close();
      await window.refresh();
    } catch (error) {
      showError(`Не вдалося зберегти персонажа: ${error.message}`);
    } finally {
      saveButton.disabled = false;
      saveButton.textContent = "Зберегти";
    }
  };

  field("character-save").addEventListener("click", window.saveCharacter);
  field("character-close").addEventListener("click", () => dialog.close());
  field("characterform").addEventListener("submit", (event) => {
    event.preventDefault();
    window.saveCharacter();
  });

  const stateClass = (value) => {
    const normalized = String(value || "").toUpperCase();
    if (["OK", "HEALTHY", "NORMAL", "SUCCEEDED", "IDLE"].includes(normalized)) return "state-ok";
    if (["FAILED", "ERROR", "OFFLINE", "UNHEALTHY"].includes(normalized)) return "state-bad";
    return "state-warn";
  };
  const ukState = (value) => ({
    OK: "Працює", HEALTHY: "Працює", NORMAL: "Нормальний", SUCCEEDED: "Завершено",
    FAILED: "Помилка", ERROR: "Помилка", OFFLINE: "Недоступний", UNHEALTHY: "Несправний",
    PENDING: "Очікує", RUNNING: "Виконується", APPLYING: "Застосовується", QUEUED: "У черзі",
    ROLLED_BACK: "Відновлено попередню версію",
  })[String(value || "").toUpperCase()] || String(value || "—");

  const queueRaw = document.querySelector("#queuestatus");
  if (queueRaw) {
    queueRaw.hidden = true;
    queueRaw.insertAdjacentHTML("afterend", '<div id="queue-friendly" class="friendly-grid"></div>');
  }
  const systemRaw = document.querySelector("#systemstatus");
  if (systemRaw) {
    systemRaw.hidden = true;
    systemRaw.insertAdjacentHTML("afterend", '<div id="system-friendly" class="friendly-grid"></div>');
  }
  const updateRaw = document.querySelector("#updatestatus");
  if (updateRaw) {
    updateRaw.hidden = true;
    updateRaw.insertAdjacentHTML("afterend", '<div id="update-friendly"></div>');
    document.querySelector("#runupdate")?.insertAdjacentHTML(
      "afterend", ' <button id="restartserver" type="button" class="secondary">Перезапустити сервер</button>');
  }

  const renderOperationalStatus = async () => {
    try {
      const status = await window.api("/api/status");
      const queue = status.queue || {}, scheduler = status.scheduler || {}, orchestration = status.orchestration || {};
      const queueTarget = document.querySelector("#queue-friendly");
      if (queueTarget) queueTarget.innerHTML = `
        <div class="friendly-card"><span>Очікують виконання</span><strong class="metric">${Number(queue.depth || 0)}</strong><small>Завдання, які ще не взяв вузол</small></div>
        <div class="friendly-card"><span>Виконуються зараз</span><strong class="metric">${Number(queue.inflight || 0)}</strong><small>Передані вузлам іще не завершені</small></div>
        <div class="friendly-card"><span>Заплановані</span><strong class="metric">${Number(scheduler.pending || 0)}</strong><small>${scheduler.next_run ? `Найближчий запуск: ${esc(scheduler.next_run)}` : "Запланованих запусків немає"}</small></div>
        <div class="friendly-card"><span>Потребують уваги</span><strong class="metric ${queue.dead_letter ? "state-bad" : "state-ok"}">${Number(queue.dead_letter || 0)}</strong><small>${queue.dead_letter ? "Нижче можна повторити невдалі завдання" : "Помилкових завдань немає"}</small></div>
        <div class="friendly-card"><span>Активні процеси</span><strong class="metric">${Number(orchestration.active_jobs || 0)}</strong><small>Активних сцен: ${Number(orchestration.active_scenes || 0)}</small></div>`;
      const systemTarget = document.querySelector("#system-friendly");
      if (systemTarget) systemTarget.innerHTML = [
        ["Ядро", status.core], ["База даних", status.postgres], ["Черга Redis", status.redis],
        ["Сховище", status.storage], ["Режим системи", status.system?.state]
      ].map(([label, value]) => `<div class="friendly-card"><span>${label}</span><strong class="metric ${stateClass(value)}">${esc(ukState(value))}</strong></div>`).join("");
    } catch (error) {
      const target = document.querySelector("#system-friendly");
      if (target) target.innerHTML = `<p class="form-error visible">Не вдалося отримати стан системи: ${esc(error.message)}</p>`;
    }
  };

  const roleLabelsUk = {
    gpu: "Генерація зображень (GPU)", text: "Генерація тексту", voice: "Синтез мовлення",
    publisher: "Публікація", backup: "Резервне копіювання", monitoring: "Моніторинг і журнали",
  };
  const settings = document.querySelector("#settings");
  if (settings) {
    const card = document.createElement("div");
    card.className = "card";
    card.id = "core-role-card";
    card.innerHTML = `<h3>Додаткові ролі цього CORE</h3>
      <p class="muted">Активуйте функції, які має виконувати цей сервер локально. Зміна запускає або зупиняє відповідні компоненти без видалення даних.</p>
      <div id="core-role-options" class="role-options">Завантаження…</div>
      <p class="role-note">GPU-роль потребує сумісної відеокарти й найбільше ресурсів. Моделі та образи можуть завантажуватися кілька хвилин.</p>
      <button id="save-core-roles" type="button">Застосувати ролі</button> <span id="core-role-result" class="muted"></span>`;
    settings.querySelector("#system-friendly")?.insertAdjacentElement("afterend", card);
    const legacyLifecycle = document.querySelector("#lifecyclestatus")?.closest(".card");
    if (legacyLifecycle) legacyLifecycle.hidden = true;
    card.insertAdjacentHTML("afterend", `<div class="card" id="friendly-lifecycle"><h3>Обслуговування системи</h3>
      <p class="muted">Резервні копії, моделі ШІ та TLS-сертифікат в одному місці.</p>
      <button type="button" onclick="createBackup()">Створити резервну копію</button> <button type="button" onclick="pullModel()">Додати модель</button> <button type="button" onclick="renewCertificate()">Оновити сертифікат</button>
      <div id="friendly-lifecycle-content">Завантаження…</div></div>`);
  }

  let knownRoleStatus = null;
  const renderRoles = async (preserveSelection = false) => {
    const options = document.querySelector("#core-role-options");
    if (!options) return;
    try {
      const selectedBefore = preserveSelection ? [...options.querySelectorAll("input:checked")].map((x) => x.value) : null;
      knownRoleStatus = await window.api("/api/system/roles");
      const active = new Set(selectedBefore || knownRoleStatus.active_roles || []);
      options.innerHTML = (knownRoleStatus.available_roles || []).map((role) => `
        <label class="role-option"><input type="checkbox" value="${esc(role.id)}" ${active.has(role.id) ? "checked" : ""}>
          <b>${esc(roleLabelsUk[role.id] || role.label || role.id)}</b>
          <small>Компоненти: ${esc((role.services || []).join(", ") || "вбудовані")}.</small></label>`).join("");
      const deployment = knownRoleStatus.deployment || {};
      document.querySelector("#core-role-result").textContent = deployment.state === "APPLYING"
        ? "Застосування змін…" : deployment.state === "FAILED" ? `Помилка: ${deployment.error || "невідома"}` : "";
    } catch (error) { options.innerHTML = `<p class="form-error visible">${esc(error.message)}</p>`; }
  };
  document.querySelector("#save-core-roles")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const roles = [...document.querySelectorAll("#core-role-options input:checked")].map((item) => item.value);
    button.disabled = true;
    document.querySelector("#core-role-result").textContent = "Передавання змін…";
    try {
      const result = await window.api("/api/system/roles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({roles})});
      document.querySelector("#core-role-result").textContent = result.message || "Зміни прийнято";
      setTimeout(() => renderRoles(false), 2500);
    } catch (error) { document.querySelector("#core-role-result").textContent = `Помилка: ${error.message}`; }
    finally { button.disabled = false; }
  });

  const phaseLabels = {
    CHECKING: "Перевірка підпису", MAINTENANCE: "Завершення активних завдань",
    DOWNLOADING: "Завантаження пакета", UPDATING: "Встановлення",
    RESTARTING: "Перезапуск сервера", VERIFYING: "Перевірка працездатності",
    RECOVERING: "Відновлення попередньої версії", NORMAL: "Готово",
  };
  const phaseFallbackProgress = {CHECKING: 10, MAINTENANCE: 25, DOWNLOADING: 40,
    UPDATING: 55, RESTARTING: 80, VERIFYING: 92, RECOVERING: 75, NORMAL: 100};
  const updateSteps = [
    ["CHECKING", "Перевірка"], ["MAINTENANCE", "Підготовка"], ["DOWNLOADING", "Завантаження"],
    ["UPDATING", "Встановлення"], ["RESTARTING", "Перезапуск"],
  ];
  const updateMessagesUk = {
    "Checking signed release manifest": "Перевіряємо підписаний маніфест оновлення",
    "Signed release manifest verified": "Підпис пакета оновлення перевірено",
    "Maintenance mode; waiting for active jobs": "Очікуємо завершення активних завдань",
    "Workers drained and queue paused": "Активні завдання завершено, чергу призупинено",
    "Downloading and verifying update package": "Завантажуємо та перевіряємо пакет оновлення",
    "Backup and package installation started": "Створюємо резервну копію та встановлюємо пакет",
    "Backup completed": "Резервну копію створено",
    "Signed release activated": "Нову версію активовано",
    "Restarting Vertep services": "Готуємо перезапуск сервісів Vertep",
    "Restarting active Vertep services": "Перезапускаємо активні сервіси Vertep",
    "Server restarted; waiting for health checks": "Сервер перезапущено, перевіряємо працездатність",
    "Update completed": "Оновлення успішно завершено",
    "Update check completed": "Перевірку оновлень завершено",
    "Server restart completed": "Перезапуск сервера завершено",
  };
  const updateMessage = (value) => updateMessagesUk[value] || value;
  let lastUpdateSnapshot = null, updateSeenRunning = false, updateOfflineSince = 0, reloadScheduled = false;
  const renderUpdateSnapshot = (value, disconnected = false) => {
    const target = document.querySelector("#update-friendly"); if (!target) return;
    let progress = Number.isFinite(Number(value.progress)) ? Number(value.progress) : (phaseFallbackProgress[value.phase] || 0);
    if (disconnected) {
      if (!updateOfflineSince) updateOfflineSince = Date.now();
      progress = Math.min(94, Math.max(progress, progress + Math.floor((Date.now() - updateOfflineSince) / 3000)));
    } else updateOfflineSince = 0;
    progress = Math.max(0, Math.min(100, Math.round(progress)));
    const failed = ["FAILED", "ROLLED_BACK"].includes(String(value.state || "").toUpperCase());
    const thresholds = [15, 30, 45, 70, 80];
    const stepMarkup = updateSteps.map(([phase, label], index) => {
      const css = progress >= (thresholds[index + 1] || 100) ? "done"
        : progress >= thresholds[index] ? "active" : "";
      return `<span class="update-step ${css}">${label}</span>`;
    }).join("");
    target.innerHTML = `<div class="update-progress-wrap">
        <div class="update-progress-head"><b>${esc(phaseLabels[value.phase] || ukState(value.state))}</b><strong>${progress}%</strong></div>
        <div class="update-progress" role="progressbar" aria-label="Прогрес оновлення" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><span class="${failed ? "failed" : ""}" style="width:${progress}%"></span></div>
      </div><div class="update-steps">${stepMarkup}</div>
      ${disconnected ? '<p class="update-offline">CORE тимчасово недоступний — сервер перезапускається. Сторінка продовжує очікувати автоматично.</p>' : ""}
      <div class="status-list">
        <div class="status-row"><span>Встановлена версія</span><b>${esc(value.current_version || "—")}</b></div>
        <div class="status-row"><span>Доступна версія</span><b>${esc(value.available_version || (value.update_available ? "нова" : "оновлень немає"))}</b></div>
        <div class="status-row"><span>Стан</span><b class="${stateClass(value.state)}">${esc(ukState(value.state))}</b></div>
        ${value.message ? `<div class="status-row"><span>Зараз виконується</span><b>${esc(updateMessage(value.message))}</b></div>` : ""}
      </div>${(value.log || []).length ? `<details><summary>Журнал оновлення</summary><ol class="event-log">${value.log.map((row) => `<li>${esc(row)}</li>`).join("")}</ol></details>` : ""}`;
  };
  const scheduleUpdateReload = (value) => {
    if (reloadScheduled || !updateSeenRunning || !["update", "restart"].includes(value.action)
        || value.state !== "SUCCEEDED" || !value.request_id) return;
    const key = `vertep-update-reloaded:${value.request_id}`;
    if (sessionStorage.getItem(key)) return;
    reloadScheduled = true;
    sessionStorage.setItem(key, "1");
    const target = document.querySelector("#update-friendly");
    target?.insertAdjacentHTML("afterbegin", '<p class="state-ok"><b>Готово. Сторінка автоматично оновиться…</b></p>');
    setTimeout(() => window.location.reload(), 1800);
  };
  const renderUpdateFriendly = async () => {
    const target = document.querySelector("#update-friendly");
    if (!target) return;
    try {
      const value = await window.api("/api/system/update");
      lastUpdateSnapshot = value;
      if (["PENDING", "RUNNING"].includes(value.state) && ["update", "restart"].includes(value.action)) updateSeenRunning = true;
      const busy = ["PENDING", "RUNNING"].includes(value.state);
      const restartButton = document.querySelector("#restartserver");
      if (restartButton) restartButton.disabled = busy || !value.enabled;
      renderUpdateSnapshot(value);
      scheduleUpdateReload(value);
    } catch (error) {
      if (lastUpdateSnapshot && updateSeenRunning) renderUpdateSnapshot(lastUpdateSnapshot, true);
      else target.innerHTML = `<p class="form-error visible">Статус оновлення недоступний: ${esc(error.message)}</p>`;
    }
  };

  window.requestSystemUpdate = async (action) => {
    const isUpdate = action === "run";
    if (isUpdate && !confirm("Завершити активні завдання, створити резервну копію, встановити оновлення та перезапустити сервер?")) return;
    try {
      const value = await window.api(`/api/system/update/${isUpdate ? "run" : "check"}`, {method: "POST"});
      if (isUpdate) updateSeenRunning = true;
      lastUpdateSnapshot = value; renderUpdateSnapshot(value); renderUpdateFriendly();
    } catch (error) { alert(error.message); renderUpdateFriendly(); }
  };
  document.querySelector("#restartserver")?.addEventListener("click", async () => {
    if (!confirm("Перезапустити активні сервіси Vertep? Сторінка буде тимчасово недоступною.")) return;
    try {
      const value = await window.api("/api/system/update/restart", {method: "POST"});
      updateSeenRunning = true; lastUpdateSnapshot = value; renderUpdateSnapshot(value); renderUpdateFriendly();
    } catch (error) { alert(error.message); }
  });

  const renderLifecycleFriendly = async () => {
    const target = document.querySelector("#friendly-lifecycle-content"); if (!target) return;
    try {
      const [backups, models, certificate] = await Promise.all(["/api/system/backups", "/api/system/models", "/api/system/certificates"].map(window.api));
      const snapshots = (backups.snapshots || []).map((item) => `<div class="status-row"><span><b>${esc(item.snapshot_id)}</b><br><small>${esc(item.created_at || "")}</small></span><button type="button" onclick="restoreBackup('${esc(item.snapshot_id)}')">Відновити</button></div>`).join("") || '<p class="muted">Резервних копій ще немає.</p>';
      const modelRows = (models.models || []).map((item) => { const name = item.name || item.model; return `<div class="status-row"><b>${esc(name)}</b><button type="button" class="danger" onclick="deleteModel('${esc(name)}')">Видалити</button></div>`; }).join("") || '<p class="muted">Локальних моделей ще немає.</p>';
      target.innerHTML = `<h4>Резервні копії</h4><div class="status-list">${snapshots}</div><h4>Моделі ШІ</h4><div class="status-list">${modelRows}</div><h4>TLS-сертифікат</h4><div class="status-list">
        <div class="status-row"><span>Стан</span><b class="${stateClass(certificate.status)}">${esc(ukState(certificate.status))}</b></div>
        <div class="status-row"><span>Кому виданий</span><b>${esc(certificate.subject || "—")}</b></div>
        <div class="status-row"><span>Дійсний до</span><b>${esc(certificate.not_after || "—")}</b></div></div>`;
    } catch (error) { target.innerHTML = `<p class="form-error visible">Не вдалося завантажити дані обслуговування: ${esc(error.message)}</p>`; }
  };

  const workflowDialog = document.querySelector("#workflowdialog");
  let friendlyWorkflow = {}, friendlyWorkflowIdentity = null;
  if (workflowDialog) {
    workflowDialog.classList.add("workflow-dialog");
    workflowDialog.innerHTML = `<h2>Сценарій обробки</h2><p class="workflow-help">Сценарій складається з послідовних вузлів. Для звичайного редагування змініть тип вузла або його параметри; JSON усього сценарію вводити не потрібно.</p>
      <div class="workflow-meta"><label>Тип сценарію<select id="workflow-kind"><option value="image">Зображення</option><option value="video">Відео</option><option value="character">Персонаж</option></select></label><label>Назва файла<input id="workflow-name" pattern="[A-Za-z0-9._-]+" required></label></div>
      <div id="workflow-nodes"></div><button id="workflow-add-node" type="button" class="secondary">Додати вузол</button>
      <p id="workflow-error" class="form-error"></p><div class="dialog-actions"><button id="workflow-save-friendly" type="button">Зберегти</button><button id="workflow-close-friendly" type="button" class="secondary">Закрити</button></div>`;
  }
  const renderWorkflowNodes = () => {
    const target = document.querySelector("#workflow-nodes"); if (!target) return;
    target.innerHTML = Object.entries(friendlyWorkflow).map(([id, node]) => `<div class="workflow-node" data-node="${esc(id)}">
      <div class="workflow-node-head"><label>№ вузла<input class="node-id" value="${esc(id)}"></label><label>Дія<input class="node-class" value="${esc(node.class_type || "")}" placeholder="Наприклад, SaveImage"></label><button type="button" class="danger node-remove">Видалити</button></div>
      <label>Параметри вузла<textarea class="node-inputs" spellcheck="false">${esc(JSON.stringify(node.inputs || {}, null, 2))}</textarea></label></div>`).join("") || '<p class="muted">Додайте перший вузол сценарію.</p>';
    target.querySelectorAll(".node-remove").forEach((button) => button.addEventListener("click", () => { delete friendlyWorkflow[button.closest(".workflow-node").dataset.node]; renderWorkflowNodes(); }));
  };
  const collectWorkflow = () => {
    const value = {};
    document.querySelectorAll("#workflow-nodes .workflow-node").forEach((row) => {
      const id = row.querySelector(".node-id").value.trim(), classType = row.querySelector(".node-class").value.trim();
      if (!id || !classType || value[id]) throw new Error("Кожен вузол повинен мати унікальний номер і назву дії");
      let inputs; try { inputs = JSON.parse(row.querySelector(".node-inputs").value || "{}"); } catch (_) { throw new Error(`Некоректні параметри у вузлі ${id}`); }
      value[id] = {class_type: classType, inputs};
    });
    return value;
  };
  window.newWorkflow = () => {
    friendlyWorkflowIdentity = null; friendlyWorkflow = {"1": {class_type: "SaveImage", inputs: {filename_prefix: "vertep"}}};
    document.querySelector("#workflow-kind").value = "image"; document.querySelector("#workflow-kind").disabled = false;
    document.querySelector("#workflow-name").value = "new-workflow.json"; document.querySelector("#workflow-name").disabled = false;
    renderWorkflowNodes(); workflowDialog.showModal();
  };
  window.editWorkflow = async (kind, name) => {
    friendlyWorkflowIdentity = {kind, name}; friendlyWorkflow = await window.api(`/api/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`);
    document.querySelector("#workflow-kind").value = kind; document.querySelector("#workflow-kind").disabled = true;
    document.querySelector("#workflow-name").value = name; document.querySelector("#workflow-name").disabled = true;
    renderWorkflowNodes(); workflowDialog.showModal();
  };
  document.querySelector("#workflow-add-node")?.addEventListener("click", () => { let id = 1; while (friendlyWorkflow[String(id)]) id += 1; friendlyWorkflow[String(id)] = {class_type: "", inputs: {}}; renderWorkflowNodes(); });
  document.querySelector("#workflow-close-friendly")?.addEventListener("click", () => workflowDialog.close());
  document.querySelector("#workflow-save-friendly")?.addEventListener("click", async () => {
    const error = document.querySelector("#workflow-error"); error.classList.remove("visible");
    try {
      const value = collectWorkflow(), kind = friendlyWorkflowIdentity?.kind || document.querySelector("#workflow-kind").value;
      let name = friendlyWorkflowIdentity?.name || document.querySelector("#workflow-name").value.trim();
      if (!name.endsWith(".json")) name += ".json";
      await window.api(`/api/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
      workflowDialog.close(); if (window.refreshConfigs) window.refreshConfigs();
    } catch (exception) { error.textContent = exception.message; error.classList.add("visible"); }
  });

  renderOperationalStatus(); renderRoles(); renderUpdateFriendly(); renderLifecycleFriendly();
  setInterval(renderOperationalStatus, 5000);
  setInterval(renderUpdateFriendly, 5000);
  setInterval(() => renderRoles(false), 10000);
  setInterval(renderLifecycleFriendly, 30000);
})();
