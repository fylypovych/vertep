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
})();
