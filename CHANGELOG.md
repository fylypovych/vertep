# Changelog

## 0.0.0.89

- Додано `AGENTS.md` — єдине правило для агентів проєкту: мова, версіонування, архітектура, сценарний агент, ComfyUI executor, FFmpeg пайплайн, Bootstrap/First Run Wizard, Safe Update System, тестування, безпека.
- Реалізовано ComfyUI executor у `worker/role_executor.py`: додано `execute_image()`/`execute_video()`, зареєстровано в `EXECUTORS`/`ROLE_TASKS`.
- Уніфіковано `worker/service.py`: тепер всі задачі викликаються через `execute_role_task()`, включаючи `image`/`video`.
- Створено `core/script_agent.py` — багатоступінчастий сценарний агент: структура → плани сцен → деталізація кожної сцени.
- Оновлено `core/pipeline.py`: переведено на `ScriptAgent`, передача конфігу персонажа, fallback на перегенерацію окремої сцени.
- Оновлено `tests/test_features.py`: замінено `LLMAdapter` на `ScriptAgent` у монкерпатчах.
- Додано сценарний агент в `AGENTS.md`: багатоступінчаста генерація, fallback, нормалізація через `ScriptDocument`.

## 0.0.0.87

- Виправлено збереження бренду: видалено неіснуючий елемент `brand-channels` з тіла PUT-запиту, через який збереження падало.
- Виправлено перевірку Telegram: `checkTelegramBot()` тепер показує реальний статус polling (`running` / `error` / `stopped`) замість фіксованого `RUNNING`.
- Замінив raw JSON textarea сценарію на дружній конструктор сцен: поля для заголовка, опису, хештегів, озвучення, список сцен з можливістю додавати/видаляти.
- Виправлено поведінку журналу оновлення: стан `<details>` тепер зберігається між оновленням snapshot.

## 0.0.0.84

- Виправлено publisher-worker: health-check тепер не падає з `PermissionError`, коли каталог storage недоступний для запису.
- Додано long polling для Telegram: мігруємо з webhook на постійний опитування оновлень.
