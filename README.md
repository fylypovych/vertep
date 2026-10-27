# Vertep

Vertep — система створення й публікації медіаконтенту з Web UI, Telegram-керуванням і розподіленим виконанням задач. Цільове середовище production — Ubuntu Server 24.04, Docker і підписані runtime-пакети GitHub Release.

CORE керує життєвим циклом Job, погодженнями, чергою та артефактами. Worker виконують генерацію тексту, зображень, голосу й відео через налаштовані providers. Монтаж через FFmpeg координується CORE; лабораторія розробки працює окремо від production.

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

Наявність коду не означає завершення всіх перевірок на реальному обладнанні. Поточний стан визначають погоджені критерії GitHub Issues: `i` — реалізація та автоматизовані перевірки, `ir` — приймання на реальному стенді. Live-публікації, фізичний GPU, аварійне відновлення та production-ізоляція LAB потребують відповідних доказів. Перехід до модульної архітектури не є дозволом видаляти чинні ролі до завершення їхніх migration gates.

## Архітектура розгортання

| Компонент | Призначення |
| --- | --- |
| CORE | API, Web UI, jobs, погодження, orchestration, dispatch і монтаж через AssemblyProvider |
| Worker / спеціалізовані вузли | Виконання задач відповідно до capabilities та доступних engines/models |
| PostgreSQL і Redis | Постійні дані, координація та черги відповідно до конфігурації |
| Proxy та службові сервіси | HTTPS, Scheduler, Dispatcher, License Manager, Certificate Manager, update agent |
| Backup і Monitoring | Резервні копії, метрики, журнали та спостереження за станом |
| LAB | Ізольований аналіз коду, перевірки й підготовка змін за правилами власника |

Практична топологія може складатися з трьох VM: CORE зі службами, GPU Worker та LAB. Text/Voice/Publisher/Backup/Monitoring розміщуються відповідно до вибраних ролей і ресурсів; три VM самі собою не гарантують готовності всіх сценаріїв. GPU VM потребує доступу до підтримуваного GPU та налаштованого runtime.

Каталог ролей — [config/node_roles.json](config/node_roles.json). Production Compose та GPU overlays — у [deploy/](deploy/). Деталі: [архітектура](docs/architecture.md), [вузли й capabilities](docs/nodes-and-capabilities.md), [контракти модулів](docs/architecture/contracts/module_contract.md).

## Production-встановлення

На цільовому Ubuntu Server 24.04:

```bash
curl -fsSL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap.sh | sudo bash
```

Bootstrap виконує перевірки хоста, готує Docker/GPU runtime, завантажує підписаний release bundle, перевіряє manifest і контрольні суми та застосовує конфігурацію ролей. Production використовує версійні артефакти й образи за digest; Git checkout або GitHub source archive не замінює runtime bundle.

Після завершення відкрийте надруковану адресу `https://SERVER-IP:8443` і пройдіть First Run Wizard. Для окремого вузла потрібні адреса CORE та дані enrollment, видані CORE. Доступність моделей і providers перевіряється для обраного розміщення.

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

Налаштований provider має бути доступним, мати потрібні моделі й credentials. `mock`/demo не доводять якість або працездатність реального engine. Підтримка адаптера не означає наявності прав API чи успішної live-публікації у конкретний account.

MoneyPrinterTurbo має окремий opt-in runtime та bridge. Його версія, сумісність, task mapping, recovery й перемикання engine перевіряються окремо; власником Job залишається Vertep.

Докладно: [конвеєр](docs/content-pipeline.md), [providers](docs/providers.md), [MoneyPrinterTurbo](docs/moneyprinter-runtime.md), [публікація](docs/publishing.md).

## Telegram

Налаштуйте токен бота в захищених інтеграціях, дозволені chat IDs і адміністративні chat IDs у налаштуваннях Telegram. Штатний режим використовує long polling із збереженням offset і не потребує публічного webhook URL.

Telegram використовується для подання контенту, перегляду стану та погоджень. Адміністративні операції залежать від прав і підтверджень. Перевірка на live-боті потребує дозволу на відповідні чати й дії. Деталі — [docs/telegram.md](docs/telegram.md).

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

Деталі: [оновлення](docs/update-system.md), [Backup/Restore](docs/backup-restore.md), [monitoring](docs/monitoring.md).

## Лабораторія розробки

LAB — окремий компонент у [lab/](lab/) зі своїм Angular UI у [deploy/lab/web/app/](deploy/lab/web/app/). Вона має workspace, audit trail, бюджети, профілі восьми агентних ролей, policy gate, GitHub governance та qualification-сценарії.

Для аналізу й виправлення коду LAB потрібна ізольована робоча копія репозиторію. Встановлений runtime не замінює вихідний код і тести. При дослідженні production-дефекту слід фіксувати встановлений release/SHA та перевіряти застосовність висновку до актуального коду.

LAB не отримує автоматичного права змінювати production, виконувати push/release або використовувати GPU. Доступ до GPU регулюється lease і пріоритетом production; активація та grants перевіряються policy gate. Створення `lab` Issues і операції з `ir` підпорядковуються окремим правилам [AGENTS.md](AGENTS.md), а не самому факту запуску панелі.

Окрема Web-панель має Docker Compose у `deploy/lab/docker-compose.web.yml`; її запуск сам собою не активує автономного executor. Інструкції, доступ і перевірки: [LAB](docs/lab.md), [Lab Web](deploy/lab/web/README.md), [qualification](docs/lab-qualification.md).

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

`install.sh` і `docker-compose.worker.yml` залишаються source/legacy-інструментами; production-середовище встановлюється через підписаний Bootstrap-потік.

## Перевірки

Базові перевірки з кореня репозиторію:

```bash
python -m compileall -q core adapters worker scripts installer tests
python -m pytest -q
git diff --check
```

Частина integration-тестів потребує PostgreSQL, Redis, Docker, FFmpeg або налаштованих зовнішніх services. Browser-тести мають окремі залежності й сценарії підготовки у [Browser E2E workflow](.github/workflows/browser-e2e.yml). Відсутня залежність чи `skip` не є доказом успішного відповідного сценарію.

Для `ir` фіксуються стенд, точний SHA/version/digests, кроки, expected/actual, статус і перевірювані артефакти. Спільний придатний доказ можна використати для кількох критеріїв; перенесення невиконаного пункту не перетворює його на PASS. Докладно — [тестування](docs/testing.md).

## Версії та публікація

Формат версії — `A.B.C.D`, з перенесенням після `99`. Наступний номер визначається за історією `main`, `VERSION` і локальними/remote tags; уже використані номери не повторюються.

Один версійний commit містить зміни, `VERSION`, запис у `CHANGELOG.md` і `releases/<VERSION>.md`; назва commit — тільки номер. Підготовка локального commit не дозволяє push.

- `пуш` — підготовка версії, перевірки, один commit і відправлення в `main`; без tag та Release.
- `реліз` — за потреби спочатку повний цикл `пуш`, потім GitHub Actions release workflow.
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
