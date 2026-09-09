# План завершення та впорядкування Web UI V2 Vertep

## 1. Мета

Перетворити поточний Web UI V2 з набору частково пов'язаних сторінок на цілісну Zero Shell адмінпанель Vertep.

Основний принцип:

> Один функціональний домен повинен мати одне логічне місце керування. Дані в Dashboard, списках, detail views та Settings повинні походити з єдиних backend-контрактів і не суперечити одне одному.

Необхідно не просто додати відсутні кнопки та сторінки, а впорядкувати інформаційну архітектуру Web UI V2 відповідно до реальної логіки Vertep.

---

# Фаза UI-A — Завдання як єдиний функціональний домен

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-A01 | Уніфікувати статистику Jobs | TODO | Dashboard, Jobs і Queue використовують однакову state/status модель; кількість READY/PENDING_APPROVAL/etc. збігається |
| UI-A02 | Переробити інформаційну архітектуру Jobs/Queue | TODO | «Черга» більше не виглядає незалежною сутністю; вона є операційним представленням життєвого циклу завдань |
| UI-A03 | Завершити Job Detail | TODO | Завдання відкривається всередині V2 layout, а не як відірвана сторінка; доступні всі дозволені state actions |
| UI-A04 | Завершити Job Editor | TODO | Topic, script/prompt, character, priority, workflow та інші редаговані параметри реально змінюються і зберігаються |
| UI-A05 | Approval flow | TODO | Для `PENDING_APPROVAL` присутня кнопка «Затвердити»; доступні відповідні Reject/Revision дії |
| UI-A06 | Delete flow | TODO | «Видалити» реально виконує DELETE, має confirmation, loading/error/success та оновлює список |
| UI-A07 | State Action Matrix | TODO | Pause/Resume/Retry/Regenerate/Cancel/Delete/Approve/Publish показуються тільки у дозволених станах |
| UI-A08 | Локалізація статусів | TODO | Backend enum не показується користувачу як `PENDING_APPROVAL`, `READY` тощо; UI використовує українські назви |
| UI-A09 | Єдиний формат дати | TODO | У всьому V2 використовується `дд.мм.рррр гг:хх`; американський формат відсутній |

---

# Фаза UI-B — Вузли, воркери, ролі та движки

Поточні Workers, Roles & Capabilities та Backends не повинні існувати як розкидані по різних частинах адмінпанелі технічні сутності.

Користувач повинен працювати насамперед із **вузлом Vertep**, бачачи його роль, можливості, встановлені модулі, обладнання та стан.

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-B01 | Об'єднати Node/Worker UX | TODO | Користувач бачить єдиний Fleet, а не розрізнені Node і Worker |
| UI-B02 | Перенести Roles & Capabilities до керування вузлами | TODO | Ролі налаштовуються у контексті конкретного вузла |
| UI-B03 | Прив'язати backends до capabilities | TODO | Видно, який backend забезпечує capability і на якому вузлі він працює |
| UI-B04 | Розширити Architecture | TODO | CORE показує не тільки напис CORE, а встановлені модулі, сервіси, ролі та capabilities |
| UI-B05 | Worker/Node Detail | TODO | Стан, роль, capabilities, hardware, metrics, modules, health та actions знаходяться на одній сторінці |
| UI-B06 | Завершити onboarding | TODO | Add Node → token → enrollment → certificate → self-test → READY проходить як один wizard |

---

# Фаза UI-C — Dashboard та системний стан

Dashboard повинен бути агрегованим представленням фактичного стану Vertep, а не окремим набором власних розрахунків.

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-C01 | Реальні системні ресурси | TODO | CPU/RAM/Disk показують фактичні значення, а не постійні `0%` |
| UI-C02 | Узгодити Job KPI | TODO | Active Jobs, Queue та Statuses відповідають фактичним Jobs |
| UI-C03 | Fleet KPI | TODO | Dashboard показує online/offline/error nodes/workers |
| UI-C04 | Architecture widget | TODO | CORE + підключені nodes + roles/modules/capabilities |
| UI-C05 | Коректно показувати відсутні метрики | TODO | Якщо метрика недоступна, UI показує «Немає даних», а не неправдивий `0%` |
| UI-C06 | Dashboard drill-down | TODO | KPI ведуть до відповідного відфільтрованого функціонального розділу |

---

# Фаза UI-D — Перебудова Settings

Settings не повинні бути складом усіх функцій, яким не знайшлося іншого місця.

Налаштування необхідно розділити за функціональними доменами.

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-D01 | Прибрати raw JSON | TODO | «Система» показує user-friendly картки/таблиці, а не JSON `/api/status` |
| UI-D02 | Розділити Settings за доменами | TODO | Загальні / Telegram / Secrets / Update / Backup / Security тощо |
| UI-D03 | Прибрати дублювання Fleet | TODO | Roles/Capabilities/Workers не розкидані між Workers і Settings |
| UI-D04 | Backends UX | TODO | Backend configuration знаходиться в логічному контексті capabilities/models без дублювання |
| UI-D05 | Update Center | TODO | Версія, available update, readiness, progress, node order, rollback та результат |
| UI-D06 | Backup & Recovery | TODO | Backup/restore/retention доступні без shell |
| UI-D07 | Secrets | TODO | Masked CRUD, readiness, rotate/delete/test без показу секрету |

---

# Фаза UI-E — Навігація та загальний Layout

Навігація повинна відображати функціональну структуру Vertep, а не внутрішню структуру backend.

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-E01 | Виправити route metadata | TODO | `/jobs` завжди показує в header «Завдання», `/characters` — «Персонажі» тощо |
| UI-E02 | Іконки sidebar | TODO | Кожен основний пункт меню має однозначну іконку |
| UI-E03 | Перегрупувати sidebar | TODO | Меню організоване за функціональними доменами, а не backend-сутностями |
| UI-E04 | Active route | TODO | Активний пункт завжди відповідає поточному URL |
| UI-E05 | Responsive/collapsed sidebar | TODO | При згортанні залишаються зрозумілі іконки + tooltip |
| UI-E06 | Українська термінологія | TODO | Прибрати змішування `Workers`, `Timeline`, `Task Type`, `Output Preset`, `Aspect Ratio`, raw enum тощо |

---

# Фаза UI-F — Профіль користувача

Поточний аватар користувача не повинен бути фактично кнопкою «Вийти».

Він має бути точкою входу до керування власним обліковим записом.

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-F01 | Profile dropdown | TODO | Клік на аватар відкриває меню користувача |
| UI-F02 | Profile page | TODO | Ім'я/логін/email/роль та дозволені параметри акаунта |
| UI-F03 | Change password | TODO | Зміна пароля через штатний UI |
| UI-F04 | Logout | TODO | «Вийти» є окремою дією внизу profile menu |
| UI-F05 | Permission-aware UX | TODO | Admin/viewer бачать лише дозволені функції |

---

# Фаза UI-G — Функціональне покриття концепції Vertep

Після виправлення базової структури необхідно провести перевірку всіх запланованих можливостей Vertep і визначити для кожної штатне місце в Web UI V2.

Перевірити UI-покриття таких функціональних доменів:

- Storyboard;
- Workflows;
- Characters;
- Brands;
- Channels;
- Publishing;
- Published content;
- Telegram;
- LLM providers;
- LLM models;
- TTS/Voice;
- GPU/Image/Video;
- Secrets;
- Updates;
- Backup/Recovery;
- Alerts;
- Logs;
- Health/Monitoring;
- First Run Wizard;
- Deployment Wizard;
- Node enrollment;
- Roles/Capabilities;
- User/permissions management.

Для кожного домену визначити:

**Backend існує → UI існує → UI дозволяє штатну операцію → state/error flow реалізований → E2E існує.**

Не можна вважати функцію реалізованою лише тому, що backend route уже існує.

Також не потрібно створювати окремий пункт sidebar для кожної сутності. Спочатку визначається інформаційна архітектура та робочий сценарій користувача.

---

# Фаза UI-H — Acceptance

| ID | Задача | Статус | Результат / acceptance |
|---|---|---|---|
| UI-H01 | Browser E2E navigation | TODO | Усі routes/header/sidebar metadata узгоджені |
| UI-H02 | Jobs E2E | TODO | create → open → edit → approve → pause/resume → delete |
| UI-H03 | Fleet E2E | TODO | add node → enroll → self-test → READY → control |
| UI-H04 | Settings E2E | TODO | Settings реально читаються та змінюються |
| UI-H05 | Profile/RBAC E2E | TODO | profile/password/logout/admin/viewer |
| UI-H06 | Cross-view consistency | TODO | Dashboard/Jobs/Queue/Fleet показують один фактичний стан |
| UI-H07 | Empty/error/loading | TODO | Жодного нескінченного skeleton; API failure має зрозумілий error state |
| UI-H08 | Localization acceptance | TODO | UI не містить випадкових англомовних назв, raw enums або американського формату дати |
| UI-H09 | Production data acceptance | TODO | Основні сторінки перевірені не лише mock responses, а й на фактичному backend Vertep |

---

# Definition of Done для Web UI V2

Функція вважається завершеною лише якщо:

1. Backend API/state contract визначений і стабільний.
2. UI працює з реальним backend contract.
3. Штатна операція виконується без shell.
4. Loading state завершується.
5. Empty state відображається коректно.
6. Backend error має зрозумілий UI error state.
7. Mutation показує progress/result.
8. Conflict та permission errors обробляються.
9. Дані не суперечать іншим представленням тієї самої сутності.
10. Відсутні raw JSON, raw enum та інші внутрішні технічні дані там, де вони не потрібні користувачу.
11. Українська локалізація та форматування однакові у всьому UI.
12. Browser E2E перевіряє основний happy path і критичні failure states.
13. E2E не використовує збільшення timeout, reload, `setTimeout`, `skip` або `xfail` як спосіб приховати дефект.
14. Angular production build проходить.
15. Існуючий функціонал інших розділів не регресує.

---

# Рекомендований порядок реалізації

1. **UI-A — Jobs**
2. **UI-B — Fleet / Nodes / Workers / Roles**
3. **UI-C — Dashboard**
4. **UI-D — Settings**
5. **UI-E — Navigation/Layout**
6. **UI-F — Profile**
7. **UI-G — повне функціональне покриття концепції**
8. **UI-H — наскрізний acceptance**

До завершення структурного етапу не виконувати великий косметичний редизайн.

Поточний пріоритет — не змінити зовнішність Web UI V2, а зробити його **функціонально цілісною адмінпанеллю Vertep**, у якій користувач може керувати всім життєвим циклом системи без shell.