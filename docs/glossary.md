# Глосарій

> Статус: чинний | Аудиторія: усі

| Термін | Значення |
|---|---|
| Job | Замовлення на один ролик: тема, персонаж, сценарій, сцени, артефакти, версії відео, receipts. Проходить стани `job-lifecycle.md`. |
| Task | Одиниця роботи для Worker: згенерувати сцену, озвучити, опублікувати. Життєвий цикл: enqueue → claim (lease) → result → retry / dead-letter. |
| Scene | Сцена сценарію: текст, голос, зображення, тривалість. |
| Character | Персонаж як конфіг у `characters/<id>/` (див. `characters-and-workflows.md`), не код. |
| Brand | Бренд/канал публікації: куди і від чийого імені виходить контент. |
| Workflow | ComfyUI-workflow (`workflows/`): модель, LoRA, sampler окремо від коду CORE. |
| Node | Вузол розгортання з роллю: core, gpu, text, voice, publisher, backup, monitoring. |
| Worker | Процес на вузлі, що бере tasks, звітує heartbeat і self-test. |
| Capability | Фактична спроможність вузла (`tts`, `image_generation`, `publish_youtube`…); заявляється при реєстрації, підтверджується self-test. |
| Role | Що встановити на вузол; capability — що вузол реально вміє. Диспетчер шукає за capability. |
| Provider / Engine / Backend | Змінний задній движок за інтерфейсом (`providers.md`): LLM, TTS, Compute, Assembly, VideoEngine, Publisher. |
| Artifact | Файл-результат (зображення, аудіо, відео, manifest) з checksum і provenance. |
| Receipt | Підтвердження публікації: платформа, remote ID, URL, час, статус. |
| Registration Token | Одноразовий токен `VT-XXXX-…` (TTL 15 хв) для підключення Worker до CORE. |
| Operation | Довга системна операція (update, backup, restore) з `operation_id`, фазами і persisted результатом. |
| System state | Глобальний стан: `NORMAL`, `MAINTENANCE`, `UPDATING`, `RECOVERING`, `READ_ONLY`, `EMERGENCY`. |
| Zero Shell | Принцип: після встановлення — усе через Web UI, без shell. |
| CORE | Центральний оркестратор (API, Dispatcher, Scheduler, Publisher-контракти, БД). |
| Runtime bundle | `vertep-runtime-<версія>.tar.gz` — підписаний пакет production-інсталяції (див. `release-contract.md`). |
| Dead-letter | Черга вичерпаних tasks після bounded retries — для ручного розбору, не втрата. |
