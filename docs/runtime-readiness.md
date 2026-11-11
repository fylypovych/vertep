# Готовність runtime: середовище й перевірки

> Статус: чинний опис runtime; кваліфікація середовища окрема | Аудиторія: розробник, ревізор | Канон: [AGENTS.md](../AGENTS.md).

## Підтримувані режими

- Bootstrap: Ubuntu Server 24.04 та 26.04, amd64/arm64, активний systemd. Наявність
  `systemctl` сама по собі недостатня: виконується запит до працюючого manager.
  Непідтримуваний manager, недоступний наявний Docker або Compose v2 відхиляються
  до зупинки служб, встановлення пакетів чи конфігурації драйверів.
- Наявний працездатний Docker ≥24 і Compose v2 зберігаються; package pins
  застосовуються лише при встановленні відсутнього runtime. Активні NVIDIA
  driver/runtime не переналаштовуються й Docker не перезапускається.
- Для чистої 26.04 зафіксовано `docker.io=29.1.3-0ubuntu4.1` та
  `docker-compose-v2=2.40.3+ds1-0ubuntu1`; пакети 24.04 залишаються окремими.
  Автоматичне встановлення AMD ROCm 6.4.1 на 26.04 не підтримується:
  потрібен попередньо налаштований сумісний runtime. Репозиторій `noble`
  на 26.04 не додається. Повна перевірка чистого сервера та GPU залишається
  окремою умовою кваліфікації; запуск у WSL її не замінює.
- Контейнеризований CORE читає host observation через окремий read-only mount;
  CORE не отримує Docker CLI/socket. Native host виконує справжній `docker info`.
- Середовище без systemd може використовувати підписаний Compose runtime під
  зовнішнім host supervisor. Штатний observer запускається як
  `python3 scripts/host-health.py --root /opt/vertep --loop`; supervisor має
  перезапускати його після збоїв. Docker context і шляхи мають збігатися з
  наявним deployment executor. Не запускати другого update executor.
  Зовнішня оркестрація також повинна виконувати штатний `apply-deployment.py`
  для запитів майстра. Сам `bootstrap.sh` без активного systemd відмовляє
  до змін; він не встановлює сторонній supervisor автоматично.

Це не гарантує підтримки довільної файлової системи, rootless Docker чи будь-якого
GPU pod. Для bootstrap залишаються перевірки ОС, архітектури, ресурсів і storage.
Фактичний контейнерний deployment без systemd потребує окремого server acceptance.

## Контракт host observation

Штатний `scripts/host-health.py` лише перевіряє daemon і, для локальної GPU ролі,
виконує мале матричне обчислення PyTorch у **ComfyUI container**. Це probe,
а не generation/self-test Worker. Observer не змінює служби й не є updater.
Systemd timer запускає його через 15 секунд після завершення попередньої перевірки;
таймаути daemon/runtime обмежені. Наявний daemon перевіряється справжнім
`docker info`; помилка або timeout дає negative observation.

Файл `config/host-health/observation.json` публікується атомарно:
`schema:1`, числовий UTC Unix `observed_at`, булеві `checks.docker`, `gpu`, `cuda`.
Вивід Docker, inspect, змінні середовища, exception text і secrets не публікуються.
CORE відхиляє missing/malformed/oversized/negative/future observation та результати
старші 90 секунд. Зупинений observer не залишає постійний зелений стан.
Права на запис залишаються host supervisor; у CORE цей підкаталог перекритий
read-only mount також під `/data/config`, щоб не залишити writable alias.

Observer, його systemd units, reader і Compose входять у signed runtime bundle.
Ручні Python hotfix або mount для конкретного провайдера не потрібні.

## Вибір ролей і готовність

Спільне джерело — застосований deployment plan, додаткові ролі та backend
інсталяції. До завершення майстра pending selection враховується окремо;
`POST /api/setup/selection` передає запит наявному deployment executor, не
створюючи адміністратора, не записуючи API keys і не завершуючи налаштування.
Applied plan належить executor і не підміняється pending selection.

CORE + GPU без Text із skip/external/OpenAI не перевіряє локальну Ollama.
Text роль або вибрана Ollama вимагає справжньої відповіді сервісу; збій блокує
готовність. Явна Text роль залишається потрібною навіть із зовнішнім backend.
Невибрані служби мають статус OPTIONAL. Зміна backend у майстрі оновлює
selection і повторює перевірки; polling і retry не очищують форму.

GPU device, runtime compute, ComfyUI HTTP та Worker registration/self-test —
окремі результати. `nvcc` не потрібен у runtime image. CORE на іншому хості
не перевіряє локальний GPU без локально вибраної GPU ролі. До завершення
First Run enrollment Worker ще не виконано: ця перевірка явно OPTIONAL;
після налаштування локальної GPU ролі CORE вимагає актуальну реєстрацію,
heartbeat і успішний self-test через наявний fail-closed Worker gate.
Pinned inventory та mTLS перевірки Worker не послаблюються.

`GET /api/health` зберігає HTTP 200 як інформаційний endpoint.
`GET /api/health/ready` повертає 503 при UNHEALTHY і залишається повною
перевіркою вибраних ролей. `GET /api/health/control` окремо перевіряє Docker,
PostgreSQL, Redis та API CORE. Саме його використовує container healthcheck CORE,
щоб вебпроксі й майстер залишалися доступними при несправності додаткової ролі.
Це не підтвердження готовності GPU/Worker і не дозвіл виконувати завдання.
До завершення First Run bootstrap очікує лише базові служби та control readiness;
для налаштованої інсталяції зберігається повна перевірка ролей. Старі signed
payload без control endpoint зберігають свій початковий контракт.
У майстрі можна явно зняти додаткову роль (зокрема GPU без обладнання) перед
перевіркою готовності. Вибрана несправна роль усе ще блокує завершення.
Майстер використовує ті самі required
checks, показує їхню обов'язковість, локалізовану причину й блокує завершення.

Автоматизовані тести контрактів не доводять роботу GPU на фізичному сервері.
Реальні First Run/inventory/mTLS/self-test і GPU-задача залишаються в #42,
#441 та відповідному scope #442. Публікація релізу — окрема команда власника.
