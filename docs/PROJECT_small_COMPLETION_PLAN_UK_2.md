# План розвитку JOB Pipeline Vertep

## 1. Мета

Побудувати повний керований життєвий цикл створення контенту:

**тема → персонаж → сценарій → затвердження → візуальна розкадровка → затвердження → відео → затвердження → публікація.**

Ключовий принцип:

> Кожен наступний етап починається тільки після явного затвердження результату попереднього етапу.

Зміни на будь-якому approval-етапі не створюють новий JOB. Вони створюють нову версію відповідного результату в межах поточного JOB.

---

# Фаза JOB-A — Створення завдання

| ID | Задача | Результат |
|---|---|---|
| JOB-A01 | Прийняти тему | Telegram приймає текстову тему |
| JOB-A02 | Вибір бренду | Користувач вибирає доступний brand |
| JOB-A03 | Вибір персонажа | Користувач вибирає character |
| JOB-A04 | Створення JOB | Створюється єдиний `job_id` |
| JOB-A05 | Збереження контексту | JOB містить topic, brand, character та необхідні параметри |
| JOB-A06 | Запуск сценарію | JOB переходить у `SCRIPT_QUEUED` |

Результат:

```text
Тема
↓
Brand
↓
Character
↓
JOB
↓
SCRIPT_QUEUED
```

---

# Фаза JOB-B — Генерація сценарію

Першим AI-результатом після створення JOB є **сценарій**, а не storyboard.

| ID | Задача | Результат |
|---|---|---|
| JOB-B01 | Prompt Builder | Формує prompt із topic + brand + character + його характеру/стилю |
| JOB-B02 | Ollama | Генерує сценарій |
| JOB-B03 | Structured output | Сценарій розділений на сцени та містить репліки/дії |
| JOB-B04 | Duration control | Сценарій відповідає цільовій тривалості ролика |
| JOB-B05 | Validation | Невалідна відповідь Ollama не передається далі |
| JOB-B06 | Versioning | Перша генерація зберігається як `script v1` |
| JOB-B07 | Telegram | Сценарій надсилається користувачу |
| JOB-B08 | Approval state | JOB переходить у `SCRIPT_PENDING_APPROVAL` |

Стани:

```text
SCRIPT_QUEUED
↓
SCRIPT_GENERATING
↓
SCRIPT_PENDING_APPROVAL
```

---

# Фаза JOB-C — Затвердження сценарію

Telegram показує сценарій і дії:

```text
✅ Затвердити
✏️ Внести зміни
🔄 Перегенерувати
❌ Відхилити
```

При **«Затвердити»**:

```text
SCRIPT_PENDING_APPROVAL
↓
SCRIPT_APPROVED
↓
STORYBOARD_QUEUED
```

При **«Внести зміни»** користувач текстом описує необхідні правки.

Наприклад:

> Зроби вступ коротшим. У другій сцені Дід має пожартувати про ціну горіхів.

Система передає Ollama:

```text
original topic
+
character
+
current script
+
revision instruction
```

Створюється:

`script v2`

Попередня версія не видаляється.

Цикл повторюється:

```text
SCRIPT_PENDING_APPROVAL
↓
SCRIPT_REVISION_REQUESTED
↓
SCRIPT_GENERATING
↓
SCRIPT_PENDING_APPROVAL
```

**Доки сценарій не отримав `SCRIPT_APPROVED`, генерація розкадровки заборонена.**

---

# Фаза JOB-D — Візуальна розкадровка

Storyboard у Vertep — це **набір зображень майбутніх сцен**, створених на основі затвердженого сценарію.

Для кожної сцени необхідно сформувати image-generation prompt із:

- затвердженого сценарію;
- опису сцени;
- персонажа;
- зовнішності персонажа;
- brand/style;
- visual continuity;
- локації;
- композиції;
- необхідного aspect ratio.

Pipeline:

```text
SCRIPT_APPROVED
↓
STORYBOARD_QUEUED
↓
підготовка scene prompts
↓
Image Generator
↓
Scene 1 image
Scene 2 image
Scene 3 image
...
↓
STORYBOARD_PENDING_APPROVAL
```

Результатом є реальні preview-зображення сцен.

---

# Фаза JOB-E — Затвердження розкадровки

Telegram надсилає користувачу сцени у зрозумілому порядку.

Наприклад:

```text
JOB 2026-000009
РОЗКАДРОВКА v1

Сцена 1
[image]

Сцена 2
[image]

Сцена 3
[image]
```

Дії:

```text
✅ Затвердити
✏️ Внести зміни
🔄 Перегенерувати
❌ Відхилити
```

Необхідно підтримати зміну **окремих сцен**, а не обов'язкову перегенерацію всієї розкадровки.

Наприклад:

> У сцені 3 прибрати пляшку зі столу.

Тоді:

```text
Scene 1 — залишається
Scene 2 — залишається
Scene 3 — regenerate
Scene 4 — залишається
```

Створюється нова версія storyboard.

Цикл триває до:

`STORYBOARD_APPROVED`.

**До цього моменту генерація відео заборонена.**

---

# Фаза JOB-F — Генерація відео

Після затвердження storyboard:

```text
STORYBOARD_APPROVED
↓
VIDEO_QUEUED
↓
VIDEO_GENERATING
```

Video pipeline використовує тільки затверджені:

- script;
- storyboard;
- character;
- voice;
- scene timing;
- visual assets.

На цьому етапі виконуються необхідні операції генерації/анімації сцен, voice/TTS, audio та фінального монтажу.

Результат:

`VIDEO_PENDING_APPROVAL`

Готовий ролик надсилається користувачу в Telegram.

---

# Фаза JOB-G — Затвердження відео

Telegram:

```text
JOB 2026-000009

Відео готове.

[VIDEO]

✅ Затвердити
✏️ Внести зміни
🔄 Перегенерувати
❌ Відхилити
```

При необхідності змін користувач описує їх текстом.

Наприклад:

> Голос занадто швидкий. Зроби паузу перед останньою фразою.

Створюється:

`video v2`

JOB залишається на video-stage.

Цикл:

```text
VIDEO_PENDING_APPROVAL
↓
VIDEO_REVISION_REQUESTED
↓
VIDEO_GENERATING
↓
VIDEO_PENDING_APPROVAL
```

до:

`VIDEO_APPROVED`.

---

# Фаза JOB-H — Публікація

Тільки:

`VIDEO_APPROVED`

відкриває publishing stage.

Pipeline:

```text
VIDEO_APPROVED
↓
PUBLISH_QUEUED
↓
PUBLISHING
↓
PUBLISHED
```

Publisher використовує налаштування brand/channel та публікує затверджений фінальний результат у визначені канали.

Не допускається автоматична публікація відео, яке не має `VIDEO_APPROVED`.

---

# Фаза JOB-I — Версійність результатів

У межах одного JOB повинні незалежно існувати:

```text
script v1
script v2
script v3

storyboard v1
storyboard v2

video v1
video v2
```

Для кожної сутності зберігати:

- version;
- created_at;
- status;
- revision instruction;
- generation parameters;
- active version;
- approved version;
- approved_at;
- approved_by.

Затверджена версія повинна бути immutable.

Нова редакція створює нову версію, а не непомітно змінює вже затверджений artifact.

---

# Фаза JOB-J — State Machine

Цільова машина станів:

```text
CREATED

SCRIPT_QUEUED
SCRIPT_GENERATING
SCRIPT_PENDING_APPROVAL
SCRIPT_REVISION_REQUESTED
SCRIPT_APPROVED
SCRIPT_FAILED

STORYBOARD_QUEUED
STORYBOARD_GENERATING
STORYBOARD_PENDING_APPROVAL
STORYBOARD_REVISION_REQUESTED
STORYBOARD_APPROVED
STORYBOARD_FAILED

VIDEO_QUEUED
VIDEO_GENERATING
VIDEO_PENDING_APPROVAL
VIDEO_REVISION_REQUESTED
VIDEO_APPROVED
VIDEO_FAILED

PUBLISH_QUEUED
PUBLISHING
PUBLISHED
PUBLISH_FAILED

CANCELLED
```

Переходи повинні бути контрольованими.

Наприклад:

```text
SCRIPT_PENDING_APPROVAL
→ STORYBOARD_QUEUED
```

напряму заборонено.

Допустимо тільки:

```text
SCRIPT_PENDING_APPROVAL
→ SCRIPT_APPROVED
→ STORYBOARD_QUEUED
```

---

# Фаза JOB-K — Telegram UX

Telegram є інтерфейсом керування JOB, але не місцем реалізації business logic.

Він повинен:

- показувати поточний результат;
- показувати зрозумілий статус;
- дозволяти approval;
- приймати revision instructions;
- показувати generation progress;
- повідомляти про failure;
- дозволяти повторну генерацію.

Користувач не повинен бачити внутрішні enum типу:

`STORYBOARD_PENDING_APPROVAL`.

Замість цього:

**«Розкадровка готова та очікує вашого затвердження.»**

---

# Фаза JOB-L — CORE/API

Всю логіку approval/revision/versioning реалізувати на рівні CORE.

Telegram і майбутній Web UI повинні працювати через однакові operations.

Концептуально:

```text
generate_script()
revise_script()
approve_script()

generate_storyboard()
revise_storyboard()
approve_storyboard()

generate_video()
revise_video()
approve_video()

publish()
```

Таким чином той самий JOB у майбутньому можна буде почати в Telegram, а продовжити у Web UI без дублювання логіки.

Це відповідає загальному напрямку Web UI Vertep, де Storyboard, Jobs, Publishing та інші функціональні домени мають працювати через реальні backend-контракти. 

---

# Фаза JOB-M — Failure та Recovery

Помилка одного production stage не повинна руйнувати JOB.

Наприклад:

```text
SCRIPT_FAILED
→ Retry Script

STORYBOARD_FAILED
→ Retry Storyboard

VIDEO_FAILED
→ Retry Video

PUBLISH_FAILED
→ Retry Publish
```

При цьому вже затверджені результати попередніх етапів повторно генерувати не потрібно.

---

# Фаза JOB-N — E2E

Основний acceptance flow:

```text
ТЕМА
↓
BRAND
↓
CHARACTER
↓
SCRIPT
↓
✏️ зміни
↓
SCRIPT v2
↓
✅ APPROVE
↓
STORYBOARD
↓
✏️ зміна Scene 3
↓
STORYBOARD v2
↓
✅ APPROVE
↓
VIDEO
↓
✏️ зміни
↓
VIDEO v2
↓
✅ APPROVE
↓
PUBLISH
↓
PUBLISHED
```

Окремо протестувати заборонені переходи:

```text
незатверджений SCRIPT → Storyboard ❌
незатверджений STORYBOARD → Video ❌
незатверджений VIDEO → Publishing ❌
```

---

# Рекомендований порядок реалізації

1. **SCRIPT generation**
2. **SCRIPT approval/revision**
3. **Versioning script**
4. **Storyboard scene model**
5. **Image generation для сцен**
6. **Storyboard Telegram preview**
7. **Storyboard approval/revision окремих сцен**
8. **Video generation**
9. **Video approval/revision**
10. **Publishing**
11. **Повний state-machine guard**
12. **Наскрізний E2E**

Перший практичний крок від поточної версії ПЗ — **прибрати автоматичний перехід нового JOB у `STORYBOARD_QUEUED` і вставити перед ним повноцінний цикл `SCRIPT_QUEUED → SCRIPT_GENERATING → SCRIPT_PENDING_APPROVAL → SCRIPT_APPROVED`.**