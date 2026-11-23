# Vertep

Vertep — система створення й публікації медіаконтенту з Web UI, Telegram-керуванням і розподіленим виконанням задач. Встановлення підтримує Ubuntu Server 24.04 та 26.04 із systemd і Docker; сумісність GPU-драйверів перевіряється окремо. Production Vertep використовує підписані runtime-пакети GitHub Release.

CORE керує життєвим циклом Job, погодженнями, чергою та артефактами. Worker виконують генерацію тексту, зображень, голосу й відео через налаштовані providers, а Publisher Worker — публікацію. LAB працює ізольовано від production.

**Статус опису:** звірено 16.11.2026 з main `0.0.3.87` ([SHA f3bbb312](https://github.com/fylypovych/vertep/commit/f3bbb3123993bc45122211409d523714a6ecf0ab)). Це версія перевіреного коду, а не твердження про останній опублікований Release або встановлену копію. Нижче окремо позначено поточну реалізацію та погоджену архітектуру, яку ще потрібно реалізувати й прийняти.

[Документація](docs/README.md) · [Задачі та критерії приймання](https://github.com/fylypovych/vertep/issues) · [Історія змін](CHANGELOG.md) · [Правила роботи з репозиторієм](AGENTS.md)

## Можливості та межі готовності

У репозиторії інтегровані:

- конвеєр «тема → сценарій → storyboard → медіа та голос → монтаж → погодження → публікація»;
- персонажі, бренди, workflow, ревізії та перевірка цілісності артефактів;
- Worker enrollment, heartbeat, capabilities, claim/result-протокол і відновлення задач;
- змінні LLM, TTS, compute та video backends, включно з опційним MoneyPrinterTurbo;
- Angular Web UI, сесії користувачів, профіль, адміністративні обмеження й Telegram polling;
- керування моделями, оновленнями, Backup/Restore, TLS, журналами та станом вузлів;
- Real Test Runner і окрема LAB із власними API, Web UI, ізольованим workspace та policy gate;
- модулі Text/Image/Audio, workflow DAG і контракти переходу архітектури.

Наявність коду не означає завершення всіх перевірок на реальному обладнанні. Погоджені критерії й докази ведуться в GitHub Issues:

- `i` — реалізація, документація та автоматизовані перевірки;
- `ir.A.B.C.D NOGPU` — реальне приймання, яке можна завершити без GPU; сервер/VM, кілька вузлів, accounts і дозволи можуть бути потрібні;
- `ir.A.B.C.D GPU` — реальне приймання фізичного GPU та залежних сценаріїв;
- `var` — архітектурні рішення; сам опис не є реалізацією.

Реальні перевірки об'єднані в [#442 NOGPU](https://github.com/fylypovych/vertep/issues/442) і [#441 GPU](https://github.com/fylypovych/vertep/issues/441). Відсутність GPU блокує відповідні GPU-підкритерії, але не всі роботи: UI, TLS, резервування, публікацію вже готового медіа й інші незалежні серверні сценарії можна приймати окремо. Для Text/Voice/LAB потрібен фактично підтримуваний runtime; позначка NOGPU сама його доступності не гарантує.

Live-публікації, GPU inference, аварійне відновлення та ізоляція LAB потребують конкретних доказів і дозволів на відповідні дії. Mock, skip, звіт моделі чи загальний green CI їх не замінюють. **Issues закриває лише власник:** LAB і CORE готують докази й висновок, без автоматичного або делегованого закриття.

## Погоджена модульна архітектура

Цільова модель — **VERTEP → ВУЗОЛ → МОДУЛЬ**. Vertep — вся система з центральним CORE; вузол — підключений сервер або VM; модуль — функція, яку адміністратор встановлює на вибраному вузлі. Центральний вузол також може містити модулі. CORE розподіляє tasks між сумісними готовими екземплярами; Worker і capabilities залишаються внутрішніми механізмами.

Шість принципів: **LAB, AI Orchestration, розподілена архітектура, Workflow Engine, Data Governance, якість**.

Наведені нижче рішення погоджені, але **не всі реалізовані в поточному runtime**:

| Напрям | Погоджена поведінка |
| --- | --- |
| Вузли й модулі | Один каталог і lifecycle: install/activate/deactivate/update/rollback/uninstall. Новий вузол приєднується до наявного CORE; другий CORE або LAB автоматично не встановлюється. Deployment-ролі поступово прибираються з UX; ролі доступу admin/brand editor/moderator зберігаються. |
| Безпека й дані | Підписані пакети та UI, ізольовані application-контейнери, версійні API/events, RBAC і secrets. Прикладні модулі мають окремі схеми у shared PostgreSQL; підпис не замінює ізоляцію. |
| Characters | Одна бібліотека й глобальні IDs, один бренд-власник. Власник активує кожного персонажа для співпраці з іншими брендами; загального перемикача бібліотеки немає. Власність не змінюється. |
| Brands і Storyboard | Окремі модулі; «Спільний контент» — вкладка Brands. Спільний матеріал потребує погоджень відповідних брендів-власників; video/hash незмінний, automatic cross-post немає. Відкликання блокує ще не розпочаті відправлення, активне завершується. |
| Publishing | Необов'язковий модуль; незалежні tasks/receipts, канали брендів і спільні connections без копіювання credentials. Inbound Telegram залишається вимикною частиною CORE, publisher використовує іншого бота. |
| FFmpeg | Фінальний монтаж виконує Worker; CORE оркеструє й перевіряє результат. У перевіреній реалізації шлях через CORE/AssemblyProvider ще збережений; перенесення — [#498](https://github.com/fylypovych/vertep/issues/498). Звичайний CPU-монтаж сам по собі GPU не потребує. |
| Monitoring | Необов'язковий модуль на центральному або іншому вузлі. Історія метрик у PostgreSQL: типово 3 дні з автоматичною ротацією; адміністратор може збільшити строк. |
| Web UI | Окремий API-клієнт із незалежним deploy/restart/update без зупинки CORE. |
| LAB | Модуль лише на окремому від production вузлі у власній VM. Обов'язкова окрема admin HTTPS-панель і необов'язкове підключення панелі до Vertep; спільні admin identity/API/налаштування. |
| Видалення модуля | До видалення активних даних — перевірений відновлюваний архів schema/config/files: без шифрування, лише для адміністратора, до ручного видалення. Він не замінює зашифровані system backup і secrets; local backup/download та окреме зберігання ключа зберігаються. |

Архітектурні вимоги, 13 окремих implementation Issues, залежності й умови загального приймання зібрані в [#487 — GLOBAL CHANGES: MODULES](https://github.com/fylypovych/vertep/issues/487). Спеціальні вимоги LAB — [#491](https://github.com/fylypovych/vertep/issues/491). #487 залишається відкритим агрегатором до виконання застосовних вимог і IR-перевірок та рішення власника; чекати закриття всіх сторонніх критеріїв #441/#442 не потрібно.

## Поточне розгортання

| Компонент | Призначення |
| --- | --- |
| CORE | API, Web UI, jobs, погодження, orchestration, dispatch і монтаж через AssemblyProvider |
| Worker / спеціалізовані вузли | Виконання задач відповідно до capabilities та доступних engines/models |
| PostgreSQL і Redis | Постійні дані, координація та черги відповідно до конфігурації |
| Proxy та службові сервіси | HTTPS, Scheduler, Dispatcher, License Manager, Certificate Manager, update agent |
| Backup і Monitoring | Резервні копії, метрики, журнали та спостереження за станом |
| LAB | Ізольований аналіз коду, перевірки й підготовка змін за правилами власника |

Наявний інсталятор і конфігурація ще використовують deployment-ролі. CORE зі службами, Worker-вузли та окрема LAB VM розміщуються за ресурсами й ізоляцією; фіксована кількість VM не є вимогою або доказом готовності. CPU-вузли можуть працювати без GPU. GPU-вузлу потрібні доступ до підтримуваного пристрою та сумісний runtime.

VMware VM створює й налаштовує адміністратор, Vertep встановлюється всередині; автоматичне створення VM продуктом не передбачене. LAB не розміщується у production VM.

Каталог ролей — [config/node_roles.json](config/node_roles.json). Production Compose та GPU overlays — у [deploy/](deploy/). Деталі: [архітектура](docs/architecture.md), [вузли й capabilities](docs/nodes-and-capabilities.md), [контракти модулів](docs/architecture/contracts/module_contract.md).

## Production-встановлення

На цільовому Ubuntu Server 24.04 або 26.04 з активним systemd:

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Під час першого запуску поточний Bootstrap пропонує Vertep або LAB на окремій VM; для автоматизації вибір задає `VERTEP_INSTALL_TARGET=vertep|lab`. Наявну інсталяцію він визначає окремо. Це поточний спосіб встановлення, який ще має перейти до єдиного керування модулями.

Для Vertep Bootstrap перевіряє хост, готує залежності, завантажує підписаний release bundle, перевіряє manifest і контрольні суми та застосовує конфігурацію. Роль і додаткові ролі CORE обираються у First Run Wizard. Production Vertep використовує версійні артефакти й образи за digest; Git checkout або GitHub source archive не замінює runtime bundle. Поточний LAB bootstrap має інший шлях зі складанням із source; перехід LAB на підписані пакети й готові образи визначено в #491.

Після завершення відкрийте надруковану адресу `https://SERVER-IP:8443` і пройдіть First Run Wizard. Для окремого вузла потрібні адреса CORE та дані enrollment, видані CORE. Доступність моделей і providers перевіряється для обраного розміщення.

Без GPU можна встановити CORE та CPU-сумісні ролі. Якщо на CORE вибрано додаткову GPU-роль, а пристрою немає, поточний bootstrap вимикає її й продовжує з іншими ролями; окремий `NODE_ROLE=gpu` без GPU відхиляється. На Ubuntu 26.04 автоматичне встановлення AMD ROCm ще не кваліфіковане: потрібен попередньо налаштований сумісний runtime. Детальні вимоги залежать від ОС, пристрою й обраних engines.

Початковий локальний TLS-сертифікат може вимагати налаштування довіри на клієнті. Постійне вимкнення перевірки TLS не є способом налаштування довіри. Порт `8443` обслуговує HTTPS.

Повторний запуск Bootstrap передбачений для відновлення перерваної інсталяції зі збереженням persistent-даних і credentials. Якщо ключі до наявних даних утрачені, їх не слід замінювати випадковими новими значеннями.

Докладно: [встановлення](docs/installation.md), [безпека](docs/security.md), [контракт release](docs/release-contract.md).

## Web UI та доступ

Основний інтерфейс — Angular-застосунок [web-v2/](web-v2/). Доступні сторінки Dashboard, Jobs/Queue, Published, Workers, Characters, Workflows, Brands, Alerts, Logs, Health, Real Tests, Settings і Profile.

Вхід відбувається через `/login` і session API. CORE має `GET`, `POST`, `DELETE /api/session`, а також endpoints профілю й пароля. Адміністративні дії перевіряються сервером; прихована кнопка не замінює перевірку прав. Зміни через сесію підпорядковуються CSRF-захисту. Node credentials використовуються окремо від адміністративної сесії.

Налаштування providers, моделей, секретів інтеграцій, оновлень і життєвого циклу доступні через відповідні розділи UI. Значення секретів не повинні потрапляти в Git, журнали чи evidence. Опис операцій — у [посібнику Web UI](docs/web-ui-guide.md).

## Медіаконвеєр і providers

Job містить сценарій, сцени, задачі, погодження та історію. Worker повертає результат через task-протокол; файли зв'язуються з артефактами й перевіряються за metadata та hash. Повтори, cancellation, stale results і recovery мають зберігати ownership задач та узгодженість ревізій. Публікація використовує погоджений результат.

Provider layer у [adapters/providers/](adapters/providers/) відділяє orchestration від конкретного engine:

| Напрям | Інтеграції / конфігурація |
| --- | --- |
| Текст | Ollama та OpenAI-compatible API; `VERTEP_LLM_PROVIDER` |
| Зображення та GPU | Worker/ComfyUI, compute providers; `VERTEP_COMPUTE_PROVIDER` |
| Голос | TTS providers, зокрема Piper/Kokoro; `TTS_PROVIDER` |
| Video engine | Native та опційні зовнішні engines; `VERTEP_VIDEO_ENGINE` |
| Монтаж | FFmpeg через AssemblyProvider |
| Публікація | Адаптери Telegram, YouTube, TikTok, Facebook, Instagram, Threads |

Налаштований provider має бути доступним, мати потрібні моделі й credentials. При відмові production GPU-провайдера система повідомляє про потребу перезапуску адміністратором; автоматичного перемикання провайдера немає. `mock`/demo не доводять якість або працездатність реального engine. Підтримка адаптера не означає наявності прав API чи успішної live-публікації у конкретний account.

MoneyPrinterTurbo має окремий opt-in runtime та bridge. Його версія, сумісність, task mapping, recovery й перемикання engine перевіряються окремо; власником Job залишається Vertep.

Докладно: [конвеєр](docs/content-pipeline.md), [providers](docs/providers.md), [MoneyPrinterTurbo](docs/moneyprinter-runtime.md), [публікація](docs/publishing.md).

## Telegram

Є дві незалежні інтеграції:

- **Вхідний бот CORE:** токен `telegram_bot_token`, дозволені й адміністративні chat IDs. Приймає запити, вибір персонажа/workflow, погодження, статуси та дозволені системні дії. Штатний long polling зберігає offset і не потребує публічного webhook URL.
- **Бот-публікатор:** окремий токен `telegram_publisher_bot_token` у налаштуваннях публікації; канал задається для бренду. Бот має право публікації в цьому каналі. Publisher Worker не отримує токен вхідного бота.

Попередні налаштування автоматично не переносяться: адміністратор задає publisher token і brand channel через UI. Без них Telegram-публікація недоступна, але оновлення Vertep не блокується. Зовнішній live-прогін потребує дозволу на конкретний канал/чат і дію.

Деталі: [Telegram](docs/telegram.md), [публікація](docs/publishing.md).

## Оновлення, дані та відновлення

Production-оновлення надходять із підписаних GitHub Releases. Процес охоплює перевірку сумісності й артефактів, maintenance/drain, резервну копію, міграції, активацію, health checks і rollback/recovery. Для кількох вузлів передбачений rolling/canary-координатор.

Jobs, персонажі, бренди, workflow та службовий стан повинні зберігатися у persistent storage. Наявність backup-файла ще не доводить можливість відновлення: приймання включає restore та читання даних відповідним consumer.

Для діагностики на встановленому хості доступний CLI:

```bash
vertep status
vertep update
vertep recover
vertep rollback
```

Команди зміни стану виконуються лише в межах дозволених операцій. Штатний користувацький шлях — Web UI; CLI не підміняє перевірку Zero Shell UX.

Для видалення інсталяції з ownership inventory передбачено `vertep remove`: повне видалення інсталяції з вибором копії (повна, лише БД, без копії); сторонні й спільні ресурси та залежності зберігаються. Реальні reboot/reinstall сценарії мають окреме стендове приймання; не плутати видалення всієї інсталяції з цільовим uninstall окремого модуля.

Деталі: [оновлення](docs/update-system.md), [Backup/Restore](docs/backup-restore.md), [видалення](docs/removal.md), [monitoring](docs/monitoring.md).

## Лабораторія розробки

**Поточна реалізація:** окремий компонент у [lab/](lab/) зі своїм Angular UI у [deploy/lab/web/app/](deploy/lab/web/app/), workspace, audit trail, бюджетами, агентними профілями й policy gate. Вбудований `lab-executor` працює в режимі audit/review/report: не змінює код і не запускає тести. Звіт містить межі прочитаного контексту; `status: ok` або відповідь моделі не означають виконання критеріїв.

Поточний bootstrap збирає LAB-контейнери на VM; панель типово слухає лише `http://127.0.0.1:8444` на цій VM, віддалений доступ — через SSH-тунель за надрукованою інсталятором інструкцією. Owner-операції використовують окремі поточні LAB credentials. Це відрізняється від погодженої цілі #491: signed package/готові образи, спільний admin-вхід і окрема HTTPS-панель без обов'язкового тунелю. Цільові можливості не слід вважати вже доступними.

Для аналізу й виправлення коду LAB потрібна ізольована робоча копія репозиторію. Встановлений runtime не замінює вихідний код і тести. При дослідженні production-дефекту слід фіксувати встановлений release/SHA та перевіряти застосовність висновку до актуального коду.

LAB не отримує автоматичного права змінювати production, виконувати push/release або використовувати GPU. Доступ до GPU регулюється lease і абсолютним пріоритетом production; idle sharing не активується до підтвердження гарантій зупинки й фізичного VRAM release. Активація та grants перевіряються policy gate.

За чинними рішеннями власника LAB не має прямого доступу до production БД, Redis, Docker socket і чужих секретів; технічне розміщення LAB state при інтеграції ще потребує узгодженого контракту. Робота з GitHub вимагає окремого доручення на відповідні дії. Старі delegated/auto-close положення в тематичних документах не переважають owner-only closure з #487/#491.

Окрема Web-панель має Docker Compose у `deploy/lab/docker-compose.web.yml`; її запуск сам собою не активує автономного executor. Інструкції, доступ і перевірки: [LAB](docs/lab.md), [Lab Web](deploy/lab/web/README.md), [qualification](docs/lab/lab-qualification.md).

## Розробка й локальний запуск

Production Bootstrap і source/demo-середовище мають різні призначення. Для локальної розробки з кореня репозиторію:

```bash
python -m pip install -r requirements.txt
python scripts/generate-env.py
# Перевірте локальну .env: providers, адреси й параметри вашого середовища.
docker compose up --build
```

Генератор створює `.env` із шаблону й унікальними секретами; не додавайте її в Git. Кореневий Compose публікує CORE на `http://localhost:8080`. Приклад `.env.example` містить demo-налаштування; реальна генерація потребує відповідних Worker, моделей і engines.

Локальне виконання генерації у CORE дозволяється тільки явною комбінацією `LOCAL_WORKER_FALLBACK=true` та `VERTEP_DEMO=true`. Цей режим не є production-конфігурацією та не зараховується як доказ виконання на Worker.

Для локального запуску окремого Worker з вихідного коду доступний `docker-compose.worker.yml`.

## Перевірки

Базові перевірки з кореня репозиторію:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m compileall -q core adapters worker scripts installer tests
python -m pytest -q
git diff --check
```

Частина integration-тестів потребує PostgreSQL, Redis, Docker, FFmpeg або налаштованих зовнішніх services. Browser-тести мають окремі залежності й сценарії підготовки у [Browser E2E workflow](.github/workflows/browser-e2e.yml). Відсутня залежність чи `skip` не є доказом успішного відповідного сценарію.

Для `ir` фіксуються стенд, точний SHA/version/digests, кроки, expected/actual, статус і перевірювані артефакти. Спільний придатний доказ можна використати для кількох критеріїв; перенесення невиконаного пункту не перетворює його на PASS. Докладно — [тестування](docs/testing.md).

## Версії та публікація

Формат версії — `A.B.C.D`, з перенесенням після `99`. Наступний номер визначається за історією `main`, `VERSION` і локальними/remote tags; уже використані номери не повторюються.

Один версійний commit містить зміни, `VERSION`, запис у `CHANGELOG.md` і `releases/<VERSION>.md`; назва commit — тільки номер. Підготовка локального commit не дозволяє push.

- `пуш` — підготовка версії, швидкі перевірки без локальних тестів, один commit з `[skip ci]` і відправлення в `main`; без tag, Release та автоматичного CI/Browser запуску.
- `тест` — підготовка версії й повний локальний pytest за AGENTS.md, commit без `[skip ci]`, відправлення в `main` та CI/Browser для цього SHA; без tag і Release.
- `реліз` — перевірка стану, за потреби повний цикл `тест`, потім GitHub Actions release workflow; без зайвого commit, якщо дерево вже чисте.
- Release workflow збирає й перевіряє артефакти конкретного SHA. Browser gate цього SHA має бути успішним до фінальної публікації.

Повноцінний release містить `vertep-runtime-<VERSION>.tar.gz`, manifest, checksums, SBOM та підпис. Автоматичні GitHub source archives runtime не замінюють.

Канон — [AGENTS.md, розділ 2](AGENTS.md); додатково — [версіювання](docs/versioning.md) і [release contract](docs/release-contract.md).

## Навігація репозиторієм

| Шлях | Вміст |
| --- | --- |
| `core/`, `core/api/` | Orchestration, стан, дані та API |
| `worker/`, `adapters/`, `publishers/` | Виконавці й інтеграції |
| `modules/` | Модулі Text/Image/Audio |
| `web-v2/` | Основна Angular-панель |
| `lab/`, `deploy/lab/` | Лабораторія та її розгортання |
| `deploy/`, `docker/`, `installer/`, `scripts/` | Runtime, образи, встановлення й службові інструменти |
| `db/`, `config/`, `monitoring/` | Міграції, схеми, ролі й спостереження |
| `tests/` | Автоматизовані перевірки |
| `docs/`, `releases/` | Документація та історія версій |

Поточний backlog і критерії приймання ведуться в GitHub Issues. Історичні звіти та release notes зберігають контекст, але не замінюють актуальні результати перевірок.
