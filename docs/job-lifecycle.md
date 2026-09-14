# Життєвий цикл Job

> Статус: чинний | Аудиторія: розробник | Канон: `AGENTS.md` §5, §10

## Основний ланцюг

```text
NEW → SCRIPTING → SCRIPT_READY → ASSET_GENERATION → ASSETS_READY →
VIDEO_GENERATION → VIDEO_READY → ASSEMBLY → READY → PUBLISHING → PUBLISHED
```

Кожен етап генерації завершується review: `*_PENDING_APPROVAL` → approve/revision. Незатверджена версія не публікується; старий approval не поширюється на нову версію.

## Бокові стани

`PAUSED`, `FAILED`, `CANCELLED`, `WAITING_FOR_SYSTEM` (система в `MAINTENANCE`/`UPDATING`).

## Глобальні стани системи

| Стан | Поведінка |
|---|---|
| `NORMAL` | Штатна робота. |
| `MAINTENANCE` | Нові задачі накопичуються (`WAITING_FOR_SYSTEM`), активні добігають. |
| `UPDATING` | Жодні задачі не запускаються. |
| `RECOVERING` | Відновлення після невдалого оновлення. |
| `READ_ONLY` | Тільки читання. |
| `EMERGENCY` | Лише аварійні дії адміністратора. |

Усі модулі орієнтуються на стан через `get_system_state()`; UI і API блокують мутації поза дозволеним станом і пояснюють причину.

## Конкурентність і надійність

- Мутації Job — з `expected_version`; stale-версія повертає HTTP 409 (захист двох сесій).
- Tasks — claim з lease і watchdog recovery; вичерпані повтори йдуть у dead-letter (`GET /api/tasks/dead-letter`, retry вручну).
- Restart CORE/Worker у визначених точках не губить job/history/versions/artifacts/receipts; approval bypass через рестарт заборонений.
- Кожен артефакт — у `manifest.json` (MIME, розмір, SHA-256, provenance сцена/task/worker/workflow); завантаження перевіряє цілісність.

> Активні доробки lifecycle (video-версії, idempotency, recovery-гейти) ведуться в GitHub Issues — див. чергу `i`/`ir` у репозиторії.
