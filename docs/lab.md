# Лабораторія Vertep

> Статус: чинний опис реалізації; готовність стенду підтверджується окремо | Аудиторія: власник, розробник | Канон: [AGENTS.md](../AGENTS.md) §4.6–§4.7.

LAB працює окремо від production CORE: policy gate, audit trail, бюджет, GPU lease, GitHub governance, CLI та окрема Angular-панель. Наявність цих компонентів не підтверджує повний автономний цикл або кваліфікацію реального стенду.

## Реалізація та доступ

- Backend і CLI — `lab/api.py`, `lab/cli.py`; однакова policy та audit trail.
- Панель — `deploy/lab/web/app/src/`, складання й запуск — [Lab Web](../deploy/lab/web/README.md).
- Compose — `deploy/lab/docker-compose.web.yml`; планувальник — `installer/vertep-lab.service` / `.timer`.
- Вбудований `lab-executor` виконує завдання аудиту, перевірки й звіту; він не змінює код і не запускає тести. Інші завдання отримують BLOCKED. Вміст файлів читається лише за явними `paths` у межах робочої копії й ліміту контексту; відповідь містить `report_only`, `code_examined`, `files_read` і `limitations` (недоступна копія, лише перелік файлів, відсутні/відхилені шляхи, обрізання). Звіт моделі й `status: ok` не є доказом PASS критеріїв. Ліміти діють під час збору, а не обрізанням уже прочитаного (i.0.0.4.81): файл читається не більше ніж на залишок ліміту контексту (`files_read[].total_chars` = `null`, якщо файл обрізано; розмір — `total_bytes`), обхід дерева зупиняється на 500 файлах (`listing_truncated`, `.git` не обходиться); `code_examined` істинне лише для прочитаного непорожнього вмісту, порожні файли позначено `empty` і в `limitations`.
- Авторизація owner-операцій: адміністратор Vertep — тим самим обліковим записом через підписане CORE твердження `X-Vertep-Identity` (#491 L3); службова автентифікація LAB — `LAB_OWNER_TOKEN` / `LAB_OWNER_IDENTITY` (CLI `--owner-token`, API `X-Lab-Owner-Token`). Grant не замінює дозвіл на production, push або release.
- Для GitHub потрібні налаштовані транспорт, репозиторій і дозвіл на конкретну дію. Dry-run не доводить зміни в GitHub.
- Межа Vertep ↔ LAB (#523, #491 L2–L5, L8):
  - **Вхід (L3).** Кнопка панелі «Увійти через Vertep» → `GET /api/lab/login` LAB → сторінка Vertep `/lab` (пункт «Лабораторія» для admin, коли в CORE задано `LAB_PANEL_URL`) → CORE `/api/lab/login` → панель із `#vertep_identity=…`. Твердження живе 5 хв, зберігається лише в пам'яті панелі й надсилається як `X-Vertep-Identity`. Проміжна сторінка `/lab` потрібна, бо cookie сесії Vertep має `SameSite=Strict`. Службовий owner token лишається запасним шляхом.
  - **Авторизація (L2).** Усі `/api/lab/*`, крім `healthz`, `readyz` і `login`, вимагають адміністратора (Vertep identity або owner token), зокрема читання стану, аудиту, grants, звітів.
  - **HTTPS (L2/L8).** Панель слухає лише TLS (`8443` у контейнері, на VM — `LAB_WEB_BIND:LAB_WEB_PORT`, типово `0.0.0.0:8444`), сертифікат — `LAB_TLS_DIR/lab.crt|lab.key` (свій або self-signed від `bootstrap-lab.sh`). SSH-тунель не потрібен; plain HTTP не публікується.
  - **Налаштування (L4).** `GET/PUT /api/lab/settings` і `lab settings show|set key=value … --owner-token …` працюють з одним сховищем `<state>/settings.json` (0600): GitHub repository/token, `LAB_RESOURCE_URL`/token, CA (PEM), Vertep URL, idle sharing. Токени лише записуються (у відповіді — `*_set`), аудит містить тільки назви полів. Збережене значення має пріоритет над `lab.env`, порожнє повертає значення з env; зміна ресурсу GPU діє після перезапуску `lab-control`.
  - **Пакет (L5).** Компонент `lab` (`deploy/lab/manifest.yaml`, `placement.target: isolated_lab`) публікується `component-release` як `lab.<версія>` з підписаним `package.json`: образи `vertep-lab-control` і `vertep-lab-web` за digest (`images.json`) і runtime compose. `bootstrap-lab.sh` перевіряє sha256 і підпис ключем `config/module-trust/publishers.json` до запуску; збирання з джерел — лише `--build-from-source`.

## Документи

- [Операції та діагностика](lab/operations.md) — поточні точки входу без активації прав за замовчуванням.
- [GPU-контракт](lab/lab-gpu-contract.md) — production-priority lease та його межі.
- [Кваліфікація R1–R6](lab/lab-qualification.md) — реальні докази; процедура не є PASS.
- [Погоджений наскрізний цикл](lab/lab-issue-cycle.md) — контракт; виконання доводиться окремо.
- [Історичний огляд](archive/lab-overview-before-448.md) і [етапи 4–7](archive/README.md) — знімки, не черга завдань.

## Межі

Production Dispatcher має пріоритет GPU; LAB використовує лише контрольований lease та дозволені безкоштовні моделі. Автономні повноваження діють лише після реалізації, перевірки й активації policy gate та в межах виданих прав. Реальні VM/GPU/24×7 перевірки мають власні докази `ir`. Поточні завдання, рішення власника та результати ведуться у GitHub Issues; історичні checklist не запускають нову роботу.
