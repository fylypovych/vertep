# Angular Web лабораторії

Окремий Angular-застосунок лабораторії (`i.0.0.2.51`, #251). Він не
використовує production CORE, `web-v2`, production volumes чи Docker socket:
єдине джерело даних — Lab API (`/api/lab/`), єдине джерело повноважень —
policy gate на backend.

Застосунок лежить у `deploy/lab/web/app` (standalone components, Angular 21,
TypeScript 5.9). Сторінки: dashboard, issues + evidence, пакети `ir`,
approvals (grants/rights/consolidation), GPU, історія рішень, звіти,
налаштування LAB і сповіщень. Вхід — «Увійти через Vertep» (#491 L3): після
повернення з Vertep твердження з `#vertep_identity=…` прибирається з адреси,
зберігається лише в пам'яті й іде заголовком `X-Vertep-Identity` у кожному
запиті (`auth.interceptor.ts`). Службовий owner token (також лише в пам'яті,
`X-Lab-Owner-Token`) — запасний шлях і, якщо введений, має пріоритет. Без
входу API відповідає 401 на все, крім `healthz`/`readyz`/`login`. UI не
вирішує, чи дозволена дія, а показує рішення policy gate.

Небезпечні дії (revoke, consolidation, зміна сповіщень, подання/закриття
`ir`) вимагають явного підтвердження в інтерфейсі й окремо перевіряються
сервером. Неавторизований клієнт отримує відмову незалежно від того, CLI це
чи Web.

## Локальна збірка

```sh
cd deploy/lab/web/app
npm ci
npm run build   # dist/vertep-lab-web/browser
```

## Docker

Image збирає Angular-застосунок усередині себе (multi-stage: `node:24-alpine`
→ `nginx:1.27-alpine`), тому зовнішній `LAB_WEB_DIST` більше не потрібен.

Штатно образи не збираються: `bootstrap-lab.sh` бере `docker-compose.web.yml`
з підписаного пакета `lab.<версія>` і задає `LAB_CONTROL_IMAGE`/`LAB_WEB_IMAGE`
за digest (#491 L5). Для розробки — оверлей збирання:

```sh
docker compose -f deploy/lab/docker-compose.web.yml \
  -f deploy/lab/docker-compose.web.build.yml up --build -d
```

Панель: `https://<адреса VM>:8444` (`LAB_WEB_BIND`, типово `0.0.0.0`;
`LAB_WEB_PORT`). Сертифікат і ключ — `LAB_TLS_DIR/lab.crt`, `lab.key`
(монтуються в `/run/lab-tls`; ключ має читати nginx, uid 101). API не
публікує host port. Compose створює власний Lab state volume і network;
Docker socket, production volumes/secrets та GPU не підключаються. Policy
activation, grants і запуск автономної роботи не виконуються. Compose не
запускає sandbox/executor; це окремі компоненти #250.

`nginx.conf` слухає лише TLS `8443` (HSTS), віддає SPA fallback, `/healthz` і
`/readyz`, а `/api/lab/` проксіює на `lab-control:8000` без додавання чи зміни
credentials. Plain HTTP `127.0.0.1:8080` усередині контейнера — тільки для
Docker healthcheck, він не публікується.

## Перевірка

```sh
python -m pytest -q tests/test_lab_web_e2e.py
python -m pytest -q tests/test_lab_web_docker.py
```

`test_lab_web_e2e.py` піднімає справжній Lab API з тимчасовим state,
production Angular build і статичний proxy з тим самим контрактом, що nginx,
після чого проходить Browser-сценарії та звіряє стан/рішення з CLI. Docker
test потребує працюючого daemon: skip без Docker не є доказом роботи image.
Обидва тести обов'язкові в CI job `lab-web-e2e`; відсутній npm або Docker там
валить job, а не пропускається.
