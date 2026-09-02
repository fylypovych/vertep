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

  const taskComposer = document.querySelector("#dashboard .grid > .card:first-child");
  const jobsPanel = document.querySelector("#jobs");
  if (taskComposer && jobsPanel) {
    taskComposer.classList.add("task-composer");
    jobsPanel.querySelector("h2")?.insertAdjacentElement("afterend", taskComposer);
  }

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
    ROLLED_BACK: "Відновлено попередню версію", EMERGENCY: "Аварійний режим",
    MAINTENANCE: "Обслуговування", RECOVERING: "Відновлення", READ_ONLY: "Лише читання",
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
  const alertsRaw = document.querySelector("#alerts");
  if (alertsRaw) {
    alertsRaw.hidden = true;
    alertsRaw.insertAdjacentHTML("afterend", '<div id="incident-friendly" class="incident-list">Завантаження…</div>');
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
      ].map(([label, value]) => `<div class="friendly-card"><span>${label}</span><strong class="metric ${stateClass(value)}">${esc(ukState(value))}</strong></div>`).join("")
        + (status.system?.state !== "NORMAL" ? `<div class="system-recovery"><b>Система потребує уваги</b><p>${esc(status.system?.reason || "Причину не записано")}</p>${status.system?.state === "EMERGENCY" ? '<button id="recover-normal" type="button">Перевірити й повернути нормальний режим</button>' : ""}</div>` : "");
      document.querySelector("#recover-normal")?.addEventListener("click", recoverNormalMode);
    } catch (error) {
      const target = document.querySelector("#system-friendly");
      if (target) target.innerHTML = `<p class="form-error visible">Не вдалося отримати стан системи: ${esc(error.message)}</p>`;
    }
  };

  const incidentLabels = {SYSTEM_STATE: "Режим системи", UPDATE_FAILED: "Оновлення", ROLE_DEPLOYMENT_FAILED: "Локальні ролі", JOB_FAILED: "Завдання", WORKER_OFFLINE: "Вузол", DEAD_LETTER_TASK: "Черга помилок"};
  const renderIncidents = async () => {
    const target = document.querySelector("#incident-friendly"); if (!target) return;
    try {
      const incidents = await window.api("/api/alerts");
      target.innerHTML = incidents.length ? incidents.map((item) => `<article class="incident ${item.severity === "error" ? "incident-error" : "incident-warning"}"><div><b>${esc(incidentLabels[item.type] || item.type || "Подія")}</b><p>${esc(updateMessage(item.message || "Потрібна увага"))}</p>${item.updated_at ? `<small>${esc(item.updated_at)}</small>` : ""}</div>${(item.details || []).length ? `<details><summary>Технічні подробиці</summary><ol class="event-log">${item.details.map((row) => `<li>${esc(row)}</li>`).join("")}</ol></details>` : ""}</article>`).join("") : '<p class="state-ok">Активних помилок і сповіщень немає.</p>';
    } catch (error) { target.innerHTML = `<p class="form-error visible">Не вдалося завантажити журнал проблем: ${esc(error.message)}</p>`; }
  };
  async function recoverNormalMode() {
    if (!confirm("Перевірити CORE, PostgreSQL і Redis та повернути систему в нормальний режим, якщо вони справні?")) return;
    try { await window.api("/api/system/recovery/normal", {method: "POST"}); await Promise.all([renderOperationalStatus(), renderIncidents()]); }
    catch (error) { alert(`Відновлення неможливе: ${error.message}`); }
  }

  const roleLabelsUk = {
    gpu: "Генерація зображень (GPU)", text: "Генерація тексту", voice: "Синтез мовлення",
    publisher: "Публікація", backup: "Резервне копіювання", monitoring: "Моніторинг і журнали",
  };
  const settings = document.querySelector("#settings");
  const workersPanel = document.querySelector("#workers");
  if (workersPanel) {
    const card = document.createElement("div");
    card.className = "card";
    card.id = "core-role-card";
    card.innerHTML = `<h3>Локальні ролі головного вузла</h3>
      <p class="muted">Керуйте функціями, які цей сервер виконує локально. Натисніть «Налаштувати ролі», щоб додати або вилучити ролі.</p>
      <div id="core-role-summary" class="role-summary">Завантаження…</div>
      <button id="open-role-wizard" type="button">Налаштувати ролі</button> <span id="core-role-result" class="muted"></span>`;
    workersPanel.querySelector("#registration")?.insertAdjacentElement("afterend", card);
  }
  if (settings) {
    const legacyLifecycle = document.querySelector("#lifecyclestatus")?.closest(".card");
    if (legacyLifecycle) legacyLifecycle.hidden = true;
    settings.querySelector("#system-friendly")?.insertAdjacentHTML("afterend", `<div class="card" id="friendly-lifecycle"><h3>Обслуговування системи</h3>
      <p class="muted">Резервні копії, моделі ШІ та TLS-сертифікат в одному місці.</p>
      <button type="button" onclick="createBackup()">Створити резервну копію</button> <button type="button" onclick="pullModel()">Додати модель</button> <button type="button" onclick="renewCertificate()">Оновити сертифікат</button>
      <div id="friendly-lifecycle-content">Завантаження…</div></div>`);
  }

  let knownRoleStatus = null;
  let roleWizardStep = 0;
  let roleWizardSelected = new Set();
  const deploymentErrorUk = (message = "") => {
    const service = message.split(":").slice(1).join(":").trim();
    if (message.includes("Selected services failed health checks")) return `Не вдалося запустити компоненти: ${service}. Перегляньте проблему у вкладці «Помилки» або зніміть відповідну роль і повторіть.`;
    if (message.includes("did not become healthy before timeout")) return "Компоненти не встигли запуститися. Перегляньте вкладку «Помилки» та повторіть застосування ролей.";
    return message || "невідома помилка";
  };
  const renderRoleSummary = async () => {
    const summary = document.querySelector("#core-role-summary");
    if (!summary) return;
    try {
      knownRoleStatus = await window.api("/api/system/roles");
      const active = knownRoleStatus.active_roles || [];
      const deployment = knownRoleStatus.deployment || {};
      const statusText = deployment.state === "APPLYING" ? "Застосування змін…"
        : deployment.state === "FAILED" ? `Помилка: ${deploymentErrorUk(deployment.error)}` : "";
      document.querySelector("#core-role-result").textContent = statusText;
      if (!active.length) {
        summary.innerHTML = `<p class="muted">Базова роль <b>CORE</b> активна. Додаткові ролі не налаштовано.</p>`;
        return;
      }
      summary.innerHTML = `<div class="role-summary-grid">${active.map((roleId) => {
        const info = (knownRoleStatus.available_roles || []).find((r) => r.id === roleId);
        const label = roleLabelsUk[roleId] || (info && info.label) || roleId;
        const services = info && info.services ? info.services.join(", ") : "вбудовані";
        return `<div class="role-summary-item"><b>${esc(label)}</b><small>Компоненти: ${esc(services)}</small></div>`;
      }).join("")}</div>`;
    } catch (error) { summary.innerHTML = `<p class="form-error visible">${esc(error.message)}</p>`; }
  };
  const openRoleWizard = async () => {
    if (!knownRoleStatus) {
      knownRoleStatus = await window.api("/api/system/roles");
    }
    roleWizardSelected = new Set(knownRoleStatus.active_roles || []);
    roleWizardStep = 1;
    renderRoleWizard();
    document.querySelector("#role-wizard-dialog")?.showModal();
  };
  const renderRoleWizard = () => {
    const dialog = document.querySelector("#role-wizard-dialog");
    if (!dialog) return;
    const step1 = dialog.querySelector("#role-wizard-step-1");
    const step2 = dialog.querySelector("#role-wizard-step-2");
    const step3 = dialog.querySelector("#role-wizard-step-3");
    const nextBtn = dialog.querySelector("#role-wizard-next");
    const backBtn = dialog.querySelector("#role-wizard-back");
    [step1, step2, step3].forEach((s) => s && (s.style.display = "none"));
    dialog.querySelectorAll(".wizard-step").forEach((el) => {
      const stepNum = parseInt(el.dataset.step);
      el.classList.remove("active", "done");
      if (stepNum === roleWizardStep) el.classList.add("active");
      else if (stepNum < roleWizardStep) el.classList.add("done");
    });
    if (roleWizardStep === 1) {
      step1.style.display = "";
      nextBtn.textContent = "Далі";
      nextBtn.disabled = false;
      backBtn.style.display = "none";
      const list = dialog.querySelector("#role-wizard-list");
      list.innerHTML = (knownRoleStatus.available_roles || []).map((role) => {
        const label = roleLabelsUk[role.id] || role.label || role.id;
        const services = (role.services || []).join(", ") || "вбудовані";
        const checked = roleWizardSelected.has(role.id) ? "checked" : "";
        return `<label class="role-wizard-option"><input type="checkbox" value="${esc(role.id)}" ${checked}>
          <b>${esc(label)}</b><small>Компоненти: ${esc(services)}</small></label>`;
      }).join("");
      list.querySelectorAll("input").forEach((input) => {
        input.onchange = () => {
          if (input.checked) roleWizardSelected.add(input.value);
          else roleWizardSelected.delete(input.value);
        };
      });
    } else if (roleWizardStep === 2) {
      step2.style.display = "";
      nextBtn.textContent = "Застосувати";
      nextBtn.disabled = false;
      backBtn.style.display = "";
      const current = knownRoleStatus.active_roles || [];
      const toAdd = [...roleWizardSelected].filter((r) => !current.includes(r));
      const toRemove = current.filter((r) => !roleWizardSelected.has(r));
      const review = dialog.querySelector("#role-wizard-review");
      let html = "";
      if (toAdd.length) html += `<p><b>Додаються:</b> ${toAdd.map((r) => esc(roleLabelsUk[r] || r)).join(", ")}</p>`;
      if (toRemove.length) html += `<p><b>Вилучаються:</b> ${toRemove.map((r) => esc(roleLabelsUk[r] || r)).join(", ")}</p>`;
      if (!toAdd.length && !toRemove.length) html = "<p>Змін немає — поточні ролі залишаються без змін.</p>";
      review.innerHTML = html;
    } else if (roleWizardStep === 3) {
      step3.style.display = "";
      nextBtn.textContent = "Готово";
      nextBtn.disabled = true;
      backBtn.style.display = "none";
      const result = dialog.querySelector("#role-wizard-result");
      result.textContent = "Передавання змін…";
      const roles = [...roleWizardSelected];
      window.api("/api/system/roles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({roles})})
        .then((result) => {
          result.textContent = result.message || "Зміни прийнято";
          renderRoleSummary();
          nextBtn.disabled = false;
        })
        .catch((error) => {
          result.textContent = `Помилка: ${error.message}`;
          nextBtn.disabled = false;
        });
    }
  };
  document.querySelector("#open-role-wizard")?.addEventListener("click", openRoleWizard);
  document.querySelector("#role-wizard-next")?.addEventListener("click", () => {
    if (roleWizardStep < 3) { roleWizardStep += 1; renderRoleWizard(); }
    else { document.querySelector("#role-wizard-dialog")?.close(); renderRoleSummary(); }
  });
  document.querySelector("#role-wizard-back")?.addEventListener("click", () => {
    if (roleWizardStep > 1) { roleWizardStep -= 1; renderRoleWizard(); }
  });
  document.querySelector("#role-wizard-close")?.addEventListener("click", () => {
    document.querySelector("#role-wizard-dialog")?.close();
  });

  const openNodeSettings = async (nodeId, nodeName) => {
    const dialog = document.querySelector("#node-settings-dialog");
    const title = document.querySelector("#node-settings-title");
    const content = document.querySelector("#node-settings-content");
    if (!dialog) return;
    title.textContent = `Налаштування вузла: ${nodeName}`;
    content.innerHTML = "Завантаження…";
    dialog.showModal();
    try {
      const nodes = await window.api("/api/nodes");
      const node = nodes.find((n) => n.node_id === nodeId) || {};
      const capabilities = (node.capabilities || []).join(", ") || "—";
      const hardware = node.hardware || {};
      content.innerHTML = `
        <div class="node-settings-grid">
          <div class="node-settings-row"><span>Ідентифікатор</span><b>${esc(node.node_id || nodeId)}</b></div>
          <div class="node-settings-row"><span>Роль</span><b>${esc(roleLabels[node.role] || node.role || "—")}</b></div>
          <div class="node-settings-row"><span>Статус</span><b>${esc(node.status || "—")}</b></div>
          <div class="node-settings-row"><span>Можливості</span><b>${esc(capabilities)}</b></div>
          <div class="node-settings-row"><span>Версія</span><b>${esc(node.version || "—")}</b></div>
          <div class="node-settings-row"><span>Архітектура</span><b>${esc(hardware.architecture || "—")}</b></div>
          <div class="node-settings-row"><span>Оперативна пам'ять</span><b>${hardware.ram_mb ? `${hardware.ram_mb} МБ` : "—"}</b></div>
          <div class="node-settings-row"><span>Відеокарта</span><b>${esc(hardware.gpu?.name || "—")}</b></div>
          <div class="node-settings-row"><span>VRAM</span><b>${hardware.gpu?.vram_mb ? `${hardware.gpu.vram_mb} МБ` : "—"}</b></div>
          <div class="node-settings-row"><span>Драйвер</span><b>${esc(hardware.gpu?.driver || "—")}</b></div>
          <div class="node-settings-row"><span>CUDA</span><b>${esc(hardware.gpu?.cuda || "—")}</b></div>
        </div>`;
    } catch (error) {
      content.innerHTML = `<p class="form-error visible">Не вдалося завантажити дані вузла: ${esc(error.message)}</p>`;
    }
  };
  window.openNodeSettings = openNodeSettings;
  document.querySelector("#node-settings-close")?.addEventListener("click", () => {
    document.querySelector("#node-settings-dialog")?.close();
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

  const brandDialog = document.querySelector("#branddialog");
  let currentBrand = null;
  if (brandDialog) {
    brandDialog.classList.add("brand-dialog");
    brandDialog.innerHTML = `<h2>Бренд</h2><form id="brand-form" class="friendly-form">
      <div class="field"><label for="brand-name">Назва бренду</label><input id="brand-name" maxlength="120" required placeholder="Наприклад, Vertep Studio"></div>
      <div class="field"><label for="brand-id">Ідентифікатор бренду</label><input id="brand-id" pattern="[a-z0-9][a-z0-9_-]{1,63}" required placeholder="vertep_studio"><small class="hint">Малі латинські літери, цифри, дефіс або підкреслення.</small></div>
      <label class="toggle"><input id="brand-enabled" type="checkbox"> Бренд активний</label>
      <div class="field wide"><label for="brand-description">Опис і стиль комунікації</label><textarea id="brand-description" placeholder="Тон, аудиторія та ключові правила бренду"></textarea></div>
      <div class="field"><label for="brand-watermark">Водяний знак</label><input id="brand-watermark" placeholder="Текст або шлях до зображення"></div>
      <div class="field"><label for="brand-language">Основна мова</label><select id="brand-language"><option value="uk">Українська</option><option value="en">Англійська</option><option value="pl">Польська</option><option value="de">Німецька</option></select></div>
      <fieldset class="wide"><legend>Публікація</legend><label class="toggle"><input id="brand-publishing-enabled" type="checkbox"> Дозволити публікацію</label></fieldset>
      <fieldset class="wide"><legend>Канали публікації</legend>
        <div id="brand-channel-list" class="channel-list">Завантаження…</div>
        <div id="brand-channel-form" class="channel-form" style="display: none">
          <div class="field"><label for="channel-type">Тип каналу</label><select id="channel-type"></select></div>
          <div class="field"><label for="channel-target">Ідентифікатор каналу</label><input id="channel-target" placeholder="@channel_name або ID"><small class="hint">Для Telegram: @channel_name. Для YouTube: channel ID.</small></div>
          <button id="channel-add-submit" type="button">Додати</button> <button id="channel-cancel" type="button" class="secondary">Скасувати</button>
        </div>
        <button id="channel-add-toggle" type="button" class="secondary">Додати канал</button>
      </fieldset>
      <p id="brand-error" class="form-error wide" role="alert"></p></form>
      <div class="dialog-actions"><button id="brand-save-friendly" type="button">Зберегти</button><button id="brand-close-friendly" type="button" class="secondary">Скасувати</button></div>`;
  }
  const channelTypeLabels = {telegram: "Telegram", youtube: "YouTube", facebook: "Facebook", tiktok: "TikTok", instagram: "Instagram", threads: "Threads"};
  let availableChannelTypes = [];
  const renderBrandChannels = async (brandId) => {
    const list = document.querySelector("#brand-channel-list");
    if (!list) return;
    if (!brandId) { list.innerHTML = "<p class='muted'>Спочатку збережіть бренд.</p>"; return; }
    try {
      const channels = await window.api(`/api/brands/${encodeURIComponent(brandId)}/channels`);
      if (!channels.length) { list.innerHTML = "<p class='muted'>Каналів ще додано.</p>"; return; }
      list.innerHTML = `<table><tr><th>Тип</th><th>Ідентифікатор</th><th>Статус</th><th>Дії</th></tr>${channels.map((ch) => `<tr><td>${esc(channelTypeLabels[ch.channel_type] || ch.channel_type)}</td><td>${esc(ch.target)}</td><td>${ch.enabled ? "Активний" : "Вимкнено"}</td><td><button type="button" class="danger" onclick="deleteBrandChannel('${esc(brandId)}','${esc(ch.channel_id)}')">Видалити</button></td></tr>`).join("")}</table>`;
    } catch (error) { list.innerHTML = `<p class="form-error visible">${esc(error.message)}</p>`; }
  };
  window.deleteBrandChannel = async (brandId, channelId) => {
    if (!confirm("Видалити цей канал?")) return;
    try {
      await window.api(`/api/channels/${encodeURIComponent(channelId)}`, {method: "DELETE"});
      renderBrandChannels(brandId);
    } catch (error) { alert(error.message); }
  };
  document.querySelector("#channel-add-toggle")?.addEventListener("click", async () => {
    const form = document.querySelector("#channel-channel-form");
    const toggle = document.querySelector("#channel-add-toggle");
    if (form.style.display === "none") {
      if (!availableChannelTypes.length) {
        availableChannelTypes = await window.api("/api/channels/types");
      }
      const select = document.querySelector("#channel-type");
      select.innerHTML = availableChannelTypes.map((t) => `<option value="${t}">${esc(channelTypeLabels[t] || t)}</option>`).join("");
      form.style.display = "";
      toggle.style.display = "none";
    }
  });
  document.querySelector("#channel-cancel")?.addEventListener("click", () => {
    document.querySelector("#brand-channel-form").style.display = "none";
    document.querySelector("#channel-add-toggle").style.display = "";
  });
  document.querySelector("#channel-add-submit")?.addEventListener("click", async () => {
    const brandId = field("brand-id").value.trim();
    const type = document.querySelector("#channel-type").value;
    const target = document.querySelector("#channel-target").value.trim();
    if (!brandId) { alert("Спочатку введіть ідентифікатор бренду"); return; }
    if (!target) { alert("Введіть ідентифікатор каналу"); return; }
    try {
      await window.api(`/api/brands/${encodeURIComponent(brandId)}/channels`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({brand_id: brandId, channel_type: type, target})
      });
      document.querySelector("#channel-target").value = "";
      document.querySelector("#brand-channel-form").style.display = "none";
      document.querySelector("#channel-add-toggle").style.display = "";
      renderBrandChannels(brandId);
    } catch (error) { alert(error.message); }
  });
  const populateBrand = (brand, isNew) => {
    currentBrand = clone(brand);
    field("brand-name").value = brand.name || "";
    field("brand-id").value = brand.id || "";
    field("brand-id").disabled = !isNew;
    field("brand-enabled").checked = brand.enabled !== false;
    field("brand-description").value = brand.metadata?.description || brand.metadata?.style_guide || "";
    field("brand-watermark").value = brand.metadata?.watermark || "";
    field("brand-language").value = brand.metadata?.language || "uk";
    field("brand-publishing-enabled").checked = brand.publishing?.enabled === true;
    field("brand-error").classList.remove("visible");
    brandDialog.querySelector("h2").textContent = isNew ? "Новий бренд" : "Редагування бренду";
    document.querySelector("#brand-channel-form").style.display = "none";
    document.querySelector("#channel-add-toggle").style.display = "";
    renderBrandChannels(brand.id);
    brandDialog.showModal(); field("brand-name").focus();
  };
  window.newBrand = () => populateBrand({id: "new_brand", name: "Новий бренд", enabled: true, metadata: {language: "uk"}, publishing: {enabled: false, channels: []}}, true);
  window.editBrand = (id) => {
    const brand = brands.find((item) => item.id === id);
    if (!brand) return window.alert("Не вдалося знайти бренд");
    populateBrand(brand, false);
  };
  document.querySelector("#brand-save-friendly")?.addEventListener("click", async () => {
    const error = field("brand-error"), form = field("brand-form");
    if (!form.reportValidity()) return;
    const id = field("brand-id").value.trim();
    const value = {...clone(currentBrand), id, name: field("brand-name").value.trim(), enabled: field("brand-enabled").checked,
      metadata: {...clone(currentBrand?.metadata), description: field("brand-description").value.trim(), watermark: field("brand-watermark").value.trim(), language: field("brand-language").value},
      publishing: {...clone(currentBrand?.publishing), enabled: field("brand-publishing-enabled").checked,
        channels: field("brand-channels").value.split(",").map((item) => item.trim()).filter(Boolean)}};
    try {
      await window.api(`/api/brands/${encodeURIComponent(id)}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
      brandDialog.close(); await window.refreshConfigs();
    } catch (exception) { error.textContent = `Не вдалося зберегти бренд: ${exception.message}`; error.classList.add("visible"); }
  });
  document.querySelector("#brand-close-friendly")?.addEventListener("click", () => brandDialog.close());
  window.deleteBrand = async (id) => {
    if (!confirm(`Видалити бренд «${id}»? Цю дію неможливо скасувати.`)) return;
    try { await window.api(`/api/brands/${encodeURIComponent(id)}`, {method: "DELETE"}); await window.refreshConfigs(); }
    catch (error) { alert(`Не вдалося видалити бренд: ${error.message}`); }
  };

  const workflowDialog = document.querySelector("#workflowdialog");
  let friendlyWorkflow = {}, friendlyWorkflowIdentity = null;
  if (workflowDialog) {
    workflowDialog.classList.add("workflow-dialog");
    workflowDialog.innerHTML = `<h2>Сценарій обробки</h2><p class="workflow-help">Сценарій складається з послідовних вузлів. Для звичайного редагування змініть тип вузла або його параметри; JSON усього сценарію вводити не потрібно.</p>
      <div class="workflow-meta"><label>Тип сценарію<select id="workflow-kind"><option value="image">Зображення</option><option value="video">Відео</option><option value="character">Персонаж</option></select></label><label>Назва файла<input id="workflow-name" pattern="[A-Za-z0-9._-]+" required></label></div>
      <datalist id="workflow-actions"><option value="CheckpointLoaderSimple"><option value="CLIPTextEncode"><option value="EmptyLatentImage"><option value="KSampler"><option value="VAEDecode"><option value="SaveImage"></datalist>
      <div id="workflow-nodes"></div><button id="workflow-add-node" type="button" class="secondary">Додати вузол</button>
      <p id="workflow-error" class="form-error"></p><div class="dialog-actions"><button id="workflow-save-friendly" type="button">Зберегти</button><button id="workflow-close-friendly" type="button" class="secondary">Закрити</button></div>`;
  }
  const workflowInputType = (value) => {
    if (Array.isArray(value) && value.length === 2 && ["string", "number"].includes(typeof value[0]) && Number.isInteger(Number(value[1]))) return "connection";
    if (typeof value === "number") return "number";
    if (typeof value === "boolean") return "boolean";
    if (value && typeof value === "object") return "json";
    return "text";
  };
  const workflowParameter = (name, value, forcedType = null) => {
    const type = forcedType || workflowInputType(value), selected = (candidate) => type === candidate ? "selected" : "";
    let control = `<input class="parameter-value" value="${esc(value ?? "")}">`;
    if (type === "number") control = `<input class="parameter-value" type="number" step="any" value="${esc(value)}">`;
    if (type === "boolean") control = `<select class="parameter-value"><option value="true" ${value ? "selected" : ""}>Так</option><option value="false" ${!value ? "selected" : ""}>Ні</option></select>`;
    if (type === "connection") control = `<span class="connection-fields"><input class="parameter-source" value="${esc(value[0])}" aria-label="Вузол-джерело"><input class="parameter-output" type="number" min="0" value="${esc(value[1])}" aria-label="Номер виходу"></span>`;
    if (type === "json") control = `<textarea class="parameter-value" spellcheck="false">${esc(JSON.stringify(value, null, 2))}</textarea>`;
    return `<div class="workflow-parameter"><input class="parameter-name" value="${esc(name)}" placeholder="Назва параметра" aria-label="Назва параметра"><select class="parameter-type" aria-label="Тип значення"><option value="text" ${selected("text")}>Текст</option><option value="number" ${selected("number")}>Число</option><option value="boolean" ${selected("boolean")}>Так / ні</option><option value="connection" ${selected("connection")}>Зв’язок із вузлом</option><option value="json" ${selected("json")}>Список або об’єкт</option></select>${control}<button type="button" class="danger parameter-remove" aria-label="Видалити параметр">×</button></div>`;
  };
  const renderWorkflowNodes = () => {
    const target = document.querySelector("#workflow-nodes"); if (!target) return;
    target.innerHTML = Object.entries(friendlyWorkflow).map(([id, node]) => `<div class="workflow-node" data-node="${esc(id)}">
      <div class="workflow-node-head"><label>№ вузла<input class="node-id" value="${esc(id)}"></label><label>Дія<input class="node-class" list="workflow-actions" value="${esc(node.class_type || "")}" placeholder="Оберіть або введіть дію"></label><button type="button" class="danger node-remove">Видалити вузол</button></div>
      <div class="workflow-parameters"><div class="workflow-parameter-title"><b>Параметри</b><small>Кожен параметр має назву, тип і значення.</small></div>${Object.entries(node.inputs || {}).map(([name, value]) => workflowParameter(name, value)).join("")}</div>
      <button type="button" class="secondary parameter-add">Додати параметр</button></div>`).join("") || '<p class="muted">Додайте перший вузол сценарію.</p>';
    target.querySelectorAll(".node-remove").forEach((button) => button.addEventListener("click", () => {
      try { friendlyWorkflow = collectWorkflow(); } catch (_) { /* Removing a broken node is still allowed. */ }
      delete friendlyWorkflow[button.closest(".workflow-node").dataset.node]; renderWorkflowNodes();
    }));
    target.querySelectorAll(".parameter-remove").forEach((button) => button.addEventListener("click", () => button.closest(".workflow-parameter").remove()));
    target.querySelectorAll(".parameter-add").forEach((button) => button.addEventListener("click", () => {
      button.previousElementSibling.insertAdjacentHTML("beforeend", workflowParameter("new_parameter", ""));
      const row = button.previousElementSibling.lastElementChild;
      row.querySelector(".parameter-remove").addEventListener("click", () => row.remove()); row.querySelector(".parameter-name").focus();
    }));
    target.onchange = (event) => {
      const select = event.target.closest?.(".parameter-type"); if (!select) return;
      const row = select.closest(".workflow-parameter"), name = row.querySelector(".parameter-name").value.trim();
      const defaults = {text: "", number: 0, boolean: false, connection: ["", 0], json: {}};
      row.insertAdjacentHTML("afterend", workflowParameter(name, defaults[select.value], select.value));
      const replacement = row.nextElementSibling; row.remove();
      replacement.querySelector(".parameter-remove").addEventListener("click", () => replacement.remove());
      replacement.querySelector(".parameter-name").focus();
    };
  };
  const collectWorkflow = () => {
    const value = {};
    document.querySelectorAll("#workflow-nodes .workflow-node").forEach((row) => {
      const id = row.querySelector(".node-id").value.trim(), classType = row.querySelector(".node-class").value.trim();
      if (!id || !classType || value[id]) throw new Error("Кожен вузол повинен мати унікальний номер і назву дії");
      const inputs = {};
      row.querySelectorAll(".workflow-parameter").forEach((parameter) => {
        const name = parameter.querySelector(".parameter-name").value.trim(), type = parameter.querySelector(".parameter-type").value;
        if (!name || Object.hasOwn(inputs, name)) throw new Error(`У вузлі ${id} параметри повинні мати унікальні назви`);
        let parameterValue = parameter.querySelector(".parameter-value")?.value ?? "";
        if (type === "number") { parameterValue = Number(parameterValue); if (!Number.isFinite(parameterValue)) throw new Error(`Параметр ${name} у вузлі ${id} має бути числом`); }
        if (type === "boolean") parameterValue = parameterValue === "true";
        if (type === "connection") parameterValue = [parameter.querySelector(".parameter-source").value.trim(), Number(parameter.querySelector(".parameter-output").value || 0)];
        if (type === "json") { try { parameterValue = JSON.parse(parameterValue); } catch (_) { throw new Error(`Список або об’єкт «${name}» у вузлі ${id} заповнено некоректно`); } }
        inputs[name] = parameterValue;
      });
      value[id] = {...clone(friendlyWorkflow[row.dataset.node]), class_type: classType, inputs};
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
  window.deleteWorkflow = async (kind, name) => {
    if (!confirm(`Видалити сценарій «${kind}/${name}»? Цю дію неможливо скасувати.`)) return;
    try { await window.api(`/api/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {method: "DELETE"}); await window.refreshConfigs(); }
    catch (error) { alert(`Не вдалося видалити сценарій: ${error.message}`); }
  };

  renderOperationalStatus(); renderRoleSummary(); renderUpdateFriendly(); renderLifecycleFriendly(); renderIncidents();
  setInterval(renderOperationalStatus, 5000);
  setInterval(renderUpdateFriendly, 5000);
  setInterval(renderIncidents, 10000);
  setInterval(() => renderRoleSummary(), 10000);
  setInterval(renderLifecycleFriendly, 30000);
})();
