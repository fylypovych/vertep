# Angular Web лабораторії

Окремий Angular-застосунок лабораторії (`i.0.0.2.51`, #251). Він не
використовує production CORE, `web-v2`, production volumes чи Docker socket:
єдине джерело даних — Lab API (`/api/lab/`), єдине джерело повноважень —
policy gate на backend.

Застосунок лежить у `deploy/lab/web/app` (standalone components, Angular 21,
TypeScript 5.9). Сторінки: dashboard, issues + evidence, пакети `ir`,
approvals (grants/rights/consolidation), GPU, історія рішень, звіти,
налаштування сповіщень. Owner token зберігається лише в `sessionStorage` і
передається заголовком `X-Lab-Owner-Token`; UI не вирішує, чи дозволена дія,
а показує рішення policy gate.

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

```sh
docker build -f deploy/lab/Dockerfile.web -t vertep-lab-web:dev .
docker compose -f deploy/lab/docker-compose.web.yml up --build -d
```

Панель: `http://127.0.0.1:8444`, порт змінюється через `LAB_WEB_PORT`. API не
публікує host port. Compose створює власний Lab state volume і network;
Docker socket, production volumes/secrets та GPU не підключаються. Policy
activation, grants і запуск автономної роботи не виконуються. Compose не
запускає sandbox/executor; це окремі компоненти #250.

`nginx.conf` віддає SPA fallback, `/healthz` і `/readyz`, а `/api/lab/`
проксіює на `lab-control:8000` без додавання чи зміни credentials. TLS
termination тут відсутній: для віддаленого доступу потрібен погоджений HTTPS
proxy, локальний listener залишається на loopback.

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
