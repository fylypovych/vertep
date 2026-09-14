# Безпека

> Статус: чинний | Аудиторія: оператор, розробник | Канон: `AGENTS.md` §22

## Секрети

- Жодних секретів у Git. Токени інтеграцій — у `integration_secret` (write-only API, presence-only читання).
- Сховище шифрується AES-256-GCM; ключ даних запечатується passphrase-механізмом (fail-closed без/з хибним passphrase); rotation/backup/restore ключа — без втрати інтеграцій.
- Файли секретів: `/opt/vertep/config/`, права `0600`. Генерація — криптографічно стійка (`openssl rand -hex`).
- Redaction на всіх межах: logs/API/DOM/receipts/metadata, включно з failure paths (перевіряється синтетичними маркерами, не реальними секретами).

## Транспорт і ідентичність

- Web TLS (`vertep.crt`/`vertep.key`) + node CA (`node-ca.crt`/`node-ca.key`); SAN/IP/DNS onboarding; expiry visibility; renew/revoke/CRL з фактичним відхиленням відкликаних сертифікатів.
- Worker enrollment — CSR + одноразовий Registration Token + JWT/Worker Secret; опційно mTLS (`NODE_MTLS_REQUIRED=true`).
- Setup-токен — з лімітом спроб і терміном дії.

## Доступ (RBAC)

- Ролі: admin / viewer; backend забороняє viewer-мутації незалежно від UI; system-state guards — для всіх mutation-ресурсів (jobs/nodes/settings/system/characters/brands/workflows/models/channels).
- UI не дає admin-прав за невідомої/нечинної політики: loading/unavailable замість дозвільного fallback.
- OAuth платформ: scopes/expiry/refresh/revoke/reconnect з readiness і зрозумілими помилками.

## Перевірка стану

API `/api/security/check` показує реальний стан encrypted-config/keys/certificates/integrations з безпечними remediation-шляхами, а не лише довжину env-значень.
