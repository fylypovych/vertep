# Docker для Web лабораторії

Контейнер обслуговує готову Angular browser-збірку окремої лабораторії та
проксіює `/api/lab/` до Lab API. Production CORE і його Web UI не використовуються.
Це Docker-частина `i.0.0.2.51` (#251), а не реалізація чи приймання самого UI.
Окремого Angular застосунку лабораторії поки немає; його розробка залишається в #251.

## Збірка image

З кореня repository:

```sh
docker build -f deploy/lab/Dockerfile.web -t vertep-lab-web:dev .
```

Image містить HTTP server/config, а frontend підключається read-only під час
запуску. Це дозволяє використовувати збірку погодженого UI без зміни Dockerfile.

## Запуск після появи Angular збірки лабораторії

`LAB_WEB_DIST` — абсолютний шлях до каталогу browser output з `index.html`
та JS/CSS/assets. Не підставляти production `web-v2` замість Lab UI.

```sh
export LAB_WEB_DIST=/absolute/path/to/lab-angular/dist/browser
docker compose -f deploy/lab/docker-compose.web.yml up --build -d
```

Панель: `http://127.0.0.1:8444`. Порт можна змінити через `LAB_WEB_PORT`.
API не публікує host port. Compose створює власний Lab state volume і network;
Docker socket, production volumes/secrets та GPU не підключаються.
Policy activation, grants і запуск автономної роботи не виконуються.
Compose не запускає sandbox/executor; це окремі компоненти #250.

`/healthz` перевіряє лише HTTP server. `/readyz` і Docker healthcheck вимагають
`index.html`; без збірки readiness повертає 503. Це не перевірка повного UI/API
acceptance. Health Lab API перевіряється окремо перед стартом Web.
Авторизація залишається на backend: proxy не додає credentials і не змінює
Authorization/cookies. Тут немає TLS termination; для віддаленого доступу потрібен
погоджений HTTPS proxy, локальний listener залишається на loopback.

## Перевірка

```sh
docker compose -f deploy/lab/docker-compose.web.yml config --quiet
python -m pytest -q tests/test_lab_web_docker.py
```

Docker integration test потребує працюючого daemon; skip без Docker не означає
успішного запуску image. Тест використовує synthetic assets/API, а не приймає
відсутній Angular UI як завершений #251.
