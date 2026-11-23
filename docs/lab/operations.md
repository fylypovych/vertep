# LAB: операції та діагностика

> Статус: чинна процедура | Аудиторія: власник, оператор лабораторії | Канон: [AGENTS.md](../../AGENTS.md) §4.6–§4.7.

## Розгортання

LAB розгортається на окремій Ubuntu VM; інструкція панелі й Compose — [Lab Web](../../deploy/lab/web/README.md). Типово `bootstrap-lab.sh` встановлює останній підписаний реліз `lab.<версія>`: завантажує `package.json` і `.sha256`, перевіряє sha256, Ed25519-підпис видавця `vertep` (`config/module-trust/publishers.json`), `placement.target: isolated_lab` і digest обох образів, і лише тоді виконує `docker compose pull` / `up --no-build` з compose-файлу пакета. Збирання з git-checkout — тільки `bootstrap-lab.sh --build-from-source` (`VERTEP_LAB_BUILD_FROM_SOURCE=1`) для розробки. Інші змінні: `VERTEP_LAB_TAG` (конкретний реліз), `LAB_WEB_BIND`/`LAB_WEB_PORT` (адреса HTTPS-панелі), `LAB_TLS_DIR` (`lab.crt`/`lab.key`; без них створюється self-signed). Bootstrap — `bootstrap-lab.sh`; його також запускає загальний `bootstrap.sh`, якщо під час першої інсталяції обрано LAB (`VERTEP_INSTALL_TARGET=lab` для автоматизації), а на сервері з уже встановленою LAB `bootstrap.sh` оновлює її до останнього релізу. Наявність запущеної панелі не активує автономний executor, policy gate або GPU sharing. Повне приймання стенду — [кваліфікація](lab-qualification.md).

## Перевірка стану

Команди виконуються в наявному ізольованому середовищі LAB. Вони не є інструкцією змінювати production.

```bash
python -m lab.cli --help
python -m lab.cli status
python -m lab.cli audit
python -m lab.cli check create_lab_issue --subject lab.0.0.0.1
```

`check` перевіряє рішення policy, не створює Issue і не надає права. GPU-команди CLI вимагають глобального `--gpu` перед підкомандою та налаштованого `LAB_RESOURCE_URL`; у його відсутність CLI може використовувати fake provider, тому його результат не є доказом physical GPU. Live GPU-операції виконуються лише в погодженому scope.

## Погодження і конфігурація

Налаштування LAB (GitHub repository/token, ресурс GPU і його токен, CA, адреса Vertep, idle sharing) — розділ «Налаштування» панелі або `python -m lab.cli settings show` / `settings set key=value … --owner-token <token>` (`key=@файл` читає секрет із файлу). Обидва шляхи пишуть один `settings.json` (0600) у state LAB і той самий запис `lab_settings:update` в audit trail (без значень). Для входу через Vertep у CORE задайте `LAB_PANEL_URL=https://<VM LAB>:8444` (`.env`; `VERTEP_LAB_PANEL_URL` для bootstrap), а в LAB — адресу Vertep (`vertep_url`).

Owner-операції CLI вимагають `LAB_OWNER_TOKEN`; API приймає підписане CORE твердження адміністратора Vertep (`X-Vertep-Identity`, #491 L3) або службовий `X-Lab-Owner-Token` з identity з `LAB_OWNER_IDENTITY`. `--approved-by` не є підтвердженням прав. Не копіювати справжній токен у звіт, Git або журнал команд. Policy gate, grants, GPU sharing і таймер активуються лише за відповідним дорученням власника.

`lab/budget.py` зберігає ліміти в `<state-dir>/budget/budget.json`, запуски — у `runs.jsonl`. Зміна бюджету або скид history не є способом обійти відмову policy. Показники читати через CLI/API; не використовувати історичні приклади з викликом приватних методів для керування робочим стендом.

## Кваліфікація

[Колектор R1–R6](lab-qualification.md) описує `scripts/qualify_lab.py --plan`, збір і незалежну валідацію доказів. План, mock, сухий запуск та підготовлений Compose не є PASS реального стенду. Кожен доказ містить SHA, середовище, очікуваний і фактичний результат, артефакти та незалежну перевірку. Критерії 24×7 не підтверджуються лише наявністю timer або UI.
