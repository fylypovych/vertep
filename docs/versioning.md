# Версіонування та публікація Vertep

> Статус: чинний | Аудиторія: розробник, агент | Канон: `AGENTS.md` §3

## Формат

`A.B.C.D` — послідовна нумерація: кожна нова версія збільшує останній компонент поточної `VERSION` (`…0.0.1.99 → 0.0.2.0`). Один commit у `main` = одна версія; назва версійного commit — лише номер (`0.0.1.70`, без суфіксів).

## Склад версії

Один commit містить: код, конфіги, тести, документацію, оновлений `VERSION`, блок `CHANGELOG.md` (`## ПРАВИЛЬНА НАЗВА: <версія>` + пункти українською) і `releases/<версія>.md` (заголовок `# Vertep <версія>`). Роздільні коміти під `VERSION`/`CHANGELOG`/нотатки і автокоміти після версійного — заборонені. Історія не переписується (ніколи: ні rebase/amend/push --force, ні рухомі tags).

## Два входи, один результат

- **Агент-команда `пуш`**: аналіз змін → нова версія → `VERSION`/`CHANGELOG`/`releases/` → перевірки → один commit → push у `main` → STOP. Без tag, Release і workflow.
- **Локальний `scripts/release.py`**: пункти в секцію `Unreleased` → `--show-next` → `release.py` (версія, нотатки, перевірки, commit, push) або `--check` для аудиту готового commit.
- **Команда `реліз`**: перевірка чистого дерева і версії → запуск GitHub Actions release workflow → build, runtime bundle, валідація → tag → GitHub Release → verification. За брудного дерева спочатку виконується логіка `пуш`.

## Workflow і gate

Release workflow працює з конкретним commit: тести → Docker images (один SHA, теги = `VERSION`) → `vertep-runtime-<версія>.tar.gz` → manifest/checksums/SBOM/підпис → перевірка артефактів → публікація. Browser E2E gate для точного SHA має бути green. Помилка workflow версій не створює; виправлення — новим `пуш` з наступною версією.

Завдання щодо версіонування й release workflow ведуться в GitHub Issues. Деталі артефактів — `release-contract.md`.
