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

- Ролі: admin / viewer; backend забороняє viewer-мутації незалежно від UI; system-state guards — для всіх mutation-ресурсів (jobs/nodes/settings/system/characters/brands/workflows/models/channels).
- UI не дає admin-прав за невідомої/нечинної політики: loading/unavailable замість дозвільного fallback.
- OAuth платформ: scopes/expiry/refresh/revoke/reconnect з readiness і зрозумілими помилками.

## Перевірка стану

API `/api/security/check` показує реальний стан encrypted-config/keys/certificates/integrations з безпечними remediation-шляхами, а не лише довжину env-значень.
