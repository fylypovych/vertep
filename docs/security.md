# Безпека

> Статус: чинний | Аудиторія: оператор, розробник | Канон: [AGENTS.md](../AGENTS.md) §0.4, §12.

## Секрети

- Жодних секретів у Git. Токени інтеграцій — у `integration_secret` (write-only API, presence-only читання).
- Сховище шифрується AES-256-GCM; ключ даних запечатується passphrase-механізмом (fail-closed без/з хибним passphrase); rotation/backup/restore ключа — без втрати інтеграцій.
- Файли секретів: `/opt/vertep/config/`, права `0600`. Генерація — криптографічно стійка (`openssl rand -hex`).
- Redaction на всіх межах: logs/API/DOM/receipts/metadata, включно з failure paths (перевіряється синтетичними маркерами, не реальними секретами).

## Транспорт і ідентичність

- Web TLS (`vertep.crt`/`vertep.key`) + node CA (`node-ca.crt`/`node-ca.key`); SAN/IP/DNS onboarding; expiry visibility; renew/revoke/CRL з фактичним відхиленням відкликаних сертифікатів.
- Worker enrollment — CSR + одноразовий Registration Token + JWT/Worker Secret; опційно mTLS (`NODE_MTLS_REQUIRED=true`).
- За `NODE_MTLS_REQUIRED=true` вузол автентифікується лише власним credential (виданий node token, `WORKER_TOKENS`/`WORKER_TOKEN_HASHES` для свого імені) разом із перевіреним сертифікатом; спільний `NODE_API_TOKEN` у цьому режимі не приймається (i.0.0.4.84). Без mTLS спільний `NODE_API_TOKEN` лишається сумісним fallback для вузлів без власного токена.
- Ідентичність вузла для mTLS (`X-Vertep-Client-DN` / `X-Vertep-Client-Serial`) proxy бере лише з TLS-сертифіката, перевіреного за node CA (`$ssl_client_verify = SUCCESS`), в усіх locations `deploy/proxy.conf` і `deploy/proxy.conf.template`; інакше заголовки не передаються в CORE, тож підставлені клієнтом значення відкидаються (i.0.0.4.72). Приймання — `tests/test_proxy_client_identity.py` на реальному nginx (`VERTEP_NGINX_EXECUTABLE`, без nginx — skip; `VERTEP_REQUIRE_PROXY_ACCEPTANCE=1` робить його обов'язковим).
- Setup-токен — з лімітом спроб і терміном дії. Невдалі спроби рахуються на реального клієнта (`SETUP_RATE_LIMIT_PER_10_MINUTES`, типово 10) і загалом на інсталяцію (`SETUP_RATE_LIMIT_GLOBAL_PER_10_MINUTES`, типово 100). Клієнт визначається за `X-Real-IP`, який proxy перезаписує, і лише коли запит прийшов від довіреного proxy (`TRUSTED_PROXY_HOSTS`, типово `proxy`; імена, IP або CIDR); від інших відправників заголовок ігнорується (i.0.0.4.82).
- Cookies сесії й CSRF отримують `Secure` за HTTPS (пряме TLS або `X-Forwarded-Proto: https` від proxy); прямий http-доступ для розробки працює без `Secure`.
- Ключ сховища секретів (`secret-store.key`) створюється одразу з правами 0600 (`O_CREAT|O_EXCL`), без вікна з правами за umask; sealing і заборона перезапису наявного ключа збережені.

## Доступ (RBAC)

- Ролі (рішення власника 19.11.2026): **admin** і **moderator**; роль viewer прибрано — збережений обліковий запис `viewer` стає модератором без брендів (лише читання). Модератор працює в межах брендів свого облікового запису (`brands` у USERS_JSON/сховищі користувачів): створює job своїх брендів, погоджує/публікує/переглядає матеріали своїх job, ухвалює рішення «Спільного контенту» за свої бренди та змінює дані модулів (Characters) своїх брендів. Налаштування, вузли, оновлення, бекапи, моделі й інші системні дії — лише admin. Backend перевіряє бренд незалежно від UI (`core/api/auth.py`, `core/api/jobs.py`); модулям Gateway передає `x-vertep-role` і `x-vertep-brands` (`*` для admin). System-state guards — для всіх mutation-ресурсів (jobs/nodes/settings/system/characters/brands/workflows/models/channels).
- LAB → CORE (#493 AC6, #491 L7): кожен виклик LAB до CORE (`/lab-lease/v1/…`: статус-probes, запит/heartbeat/звільнення оренди GPU, inference), включно з відмовами й неавтентифікованими спробами, записується в audit trail платформи (`module-audit.jsonl`, дія `lab.lease`, path/status/outcome/reason/lease).
- Бренди (#500, рішення власника 19.11.2026): єдиний власник брендів, каналів і політик публікації — модуль Brands (власна schema; канал↔бренди M:N без копій підключення; політика бренду `automatic`/`approval_required` і перевизначення каналу `inherit`/`automatic`/`approval_required`). Коли модуль READY, CORE (`core/brand_store.py`: API брендів/каналів, монтаж, сторіборд, Telegram, цілі публікації) працює лише через нього; без відповіді модуля — 503, без тихого повернення до старих даних. Наявні бренди й канали переносить admin: `POST /api/brands/migrate-to-module` (ідемпотентно, ID зберігаються, старі файли не видаляються). Модератор змінює лише свої бренди; створення/видалення брендів і міграція — admin.
- Публікація (#502, рішення власника 19.11.2026): модуль Publishing — лише інтерфейс і дозволені дії редактора (без власної бази, каналів і шляху відправлення); канали, прив'язки й політики — у Brands; оркестрація, погодження, статуси й квитанції — CORE; відправлення — Worker. Дію з модуля CORE приймає лише з підписаним Gateway короткоживучим контекстом користувача (`x-vertep-user-context`, прив'язаний до модуля, 2 хв); CORE сам заново визначає роль і бренди користувача й записує в аудит справжнього користувача.
- Облікові дані каналу (#502): один набір секретів на канал (`channel:<id>:<name>` у сховищі секретів), спільний для всіх брендів каналу; задача публікації отримує лише секрети свого каналу. Канал без секретів — публікацію заблоковано з поясненням. Глобальні облікові дані платформи — лише за явним дозволом адміністратора на конкретному каналі (`PUT /api/channels/<id>/credentials {"use_global_credentials": true}`), з аудитом і попередженням; автоматичної міграції токенів немає. Автоматична публікація (політика `automatic` каналу або бренду) йде звичайним шляхом з усіма перевірками.
- «Спільний контент» (#500): job бренду A з персонажем бренду B фіксує B як власника (`character_owner_brand`). Публікація потребує погодження B саме активної версії (version + sha256); pending не має строку і публікацію не дозволяє. Рішення — append-only історія (actor/time/decision/version/hash/brand). Перевірка повторюється на межі старту відправлення: revoke чи зміна файлу зупиняють ще не розпочаті відправлення, розпочате завершується, опубліковане не видаляється.
- UI не дає admin-прав за невідомої/нечинної політики: loading/unavailable замість дозвільного fallback.
- OAuth платформ: scopes/expiry/refresh/revoke/reconnect з readiness і зрозумілими помилками.

## Перевірка стану

API `/api/security/check` показує реальний стан encrypted-config/keys/certificates/integrations з безпечними remediation-шляхами, а не лише довжину env-значень.
