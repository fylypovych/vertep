# Лабораторія Vertep

> Статус: чинний опис реалізації; готовність стенду підтверджується окремо | Аудиторія: власник, розробник | Канон: [AGENTS.md](../AGENTS.md) §4.6–§4.7.

LAB працює окремо від production CORE: policy gate, audit trail, бюджет, GPU lease, GitHub governance, CLI та окрема Angular-панель. Наявність цих компонентів не підтверджує повний автономний цикл або кваліфікацію реального стенду.

## Реалізація та доступ

- Backend і CLI — `lab/api.py`, `lab/cli.py`; однакова policy та audit trail.
- Панель — `deploy/lab/web/app/src/`, складання й запуск — [Lab Web](../deploy/lab/web/README.md).
- Compose — `deploy/lab/docker-compose.web.yml`; планувальник — `installer/vertep-lab.service` / `.timer`.
- Вбудований `lab-executor` виконує завдання аудиту, перевірки й звіту; він не змінює код і не запускає тести. Інші завдання отримують BLOCKED. Вміст файлів читається лише за явними `paths` у межах робочої копії й ліміту контексту; відповідь містить `report_only`, `code_examined`, `files_read` і `limitations` (недоступна копія, лише перелік файлів, відсутні/відхилені шляхи, обрізання). Звіт моделі й `status: ok` не є доказом PASS критеріїв.
- Авторизація owner-операцій — `LAB_OWNER_TOKEN` / `LAB_OWNER_IDENTITY`; CLI приймає `--owner-token`, API — `X-Lab-Owner-Token`. Grant не замінює дозвіл на production, push або release.
- Для GitHub потрібні налаштовані транспорт, репозиторій і дозвіл на конкретну дію. Dry-run не доводить зміни в GitHub.

## Документи

- [Операції та діагностика](lab/operations.md) — поточні точки входу без активації прав за замовчуванням.
- [GPU-контракт](lab/lab-gpu-contract.md) — production-priority lease та його межі.
- [Кваліфікація R1–R6](lab/lab-qualification.md) — реальні докази; процедура не є PASS.
- [Погоджений наскрізний цикл](lab/lab-issue-cycle.md) — контракт; виконання доводиться окремо.
- [Історичний огляд](archive/lab-overview-before-448.md) і [етапи 4–7](archive/README.md) — знімки, не черга завдань.

## Межі

Production Dispatcher має пріоритет GPU; LAB використовує лише контрольований lease та дозволені безкоштовні моделі. Автономні повноваження діють лише після реалізації, перевірки й активації policy gate та в межах виданих прав. Реальні VM/GPU/24×7 перевірки мають власні докази `ir`. Поточні завдання, рішення власника та результати ведуться у GitHub Issues; історичні checklist не запускають нову роботу.
