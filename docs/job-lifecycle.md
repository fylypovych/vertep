# Життєвий цикл Job

> Статус: чинний | Аудиторія: розробник | Канон: [AGENTS.md](../AGENTS.md) §3.1, §14–§15.

## Основний ланцюг

```text
NEW → SCRIPT_QUEUED → SCRIPT_GENERATING → SCRIPT_PENDING_APPROVAL → SCRIPT_APPROVED
→ STORYBOARD_QUEUED → STORYBOARD_GENERATING → STORYBOARD_PENDING_APPROVAL
→ STORYBOARD_APPROVED → ASSET_GENERATION → TTS_GENERATING → TTS_READY
→ VIDEO_GENERATION → VIDEO_PENDING_APPROVAL → VIDEO_APPROVED
→ ASSEMBLY → READY → PUBLISHING → PUBLISHED
```

Це спрощений основний шлях, а не перелік усіх дозволених переходів. Джерело фактичних станів і переходів — [JobStatus та JOB_STATE_TRANSITIONS](../core/models.py). Storyboard також має preview-стан `STORYBOARD_READY`; залежно від шляху можливі `ASSETS_READY`, `VIDEO_READY`, revision/failure-переходи та перехід TTS безпосередньо до assembly. Legacy `SCRIPTING`, `SCRIPT_READY`, `PENDING_APPROVAL` залишаються в enum, але не замінюють поточний script workflow.

Етапи сценарію, storyboard та відео мають review: `*_PENDING_APPROVAL` → approve/revision. Незатверджена версія не публікується; старий approval не поширюється на нову версію.

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
