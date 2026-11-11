# Vertep — карта документації

> Статус: чинний індекс | Аудиторія: усі | Канон: [AGENTS.md](../AGENTS.md).

Нормативні повноваження, заборони й робочі процедури агентів визначає AGENTS.md. Настанови описують реалізацію та операторські дії; їх звіряють із кодом і конфігурацією. Матеріал приймання підтверджує лише власний SHA, середовище та критерії. Історичний аудит не визначає поточний стан. Scope і черга робіт ведуться виключно в GitHub Issues.

## Порядок читання

[Огляд](overview.md) → [глосарій](glossary.md) → [архітектура](architecture.md). Оператору: [встановлення](installation.md), [Web UI](web-ui-guide.md), [оновлення](update-system.md). Розробнику: [конвеєр](content-pipeline.md), [providers](providers.md), [тестування](testing.md).

## Чинні настанови

- [removal.md](removal.md) — Видалення інсталяції Vertep.
- [architecture.md](architecture.md) — Архітектура.
- [backup-restore.md](backup-restore.md) — Backup / Restore.
- [characters-and-workflows.md](characters-and-workflows.md) — Персонажі та workflow.
- [content-pipeline.md](content-pipeline.md) — Контентний конвеєр.
- [core-generation-gate.md](core-generation-gate.md) — CORE Generation Gate — архітектурний контракт.
- [glossary.md](glossary.md) — Глосарій.
- [installation.md](installation.md) — Встановлення.
- [job-lifecycle.md](job-lifecycle.md) — Життєвий цикл Job.
- [lab.md](lab.md) — Лабораторія Vertep.
- [moneyprinter-runtime.md](moneyprinter-runtime.md) — Ізольований runtime MoneyPrinterTurbo.
- [monitoring.md](monitoring.md) — Моніторинг.
- [nodes-and-capabilities.md](nodes-and-capabilities.md) — Вузли та capabilities.
- [overview.md](overview.md) — Vertep — огляд.
- [providers.md](providers.md) — Provider / Adapter Layer.
- [publishing.md](publishing.md) — Публікація.
- [release-contract.md](release-contract.md) — Підписаний контракт runtime-релізу Vertep.
- [release-key-ceremony.md](release-key-ceremony.md) — Release Key Ceremony.
- [runtime-readiness.md](runtime-readiness.md) — Готовність runtime: середовище й перевірки.
- [security.md](security.md) — Безпека.
- [telegram.md](telegram.md) — Telegram.
- [testing.md](testing.md) — Тестування.
- [update-system.md](update-system.md) — Safe Update System.
- [versioning.md](versioning.md) — Версіонування та публікація Vertep.
- [web-ui-guide.md](web-ui-guide.md) — Web UI — гід.

## Контракти та архітектурні review

- [architecture/contracts/artifact_contract.md](architecture/contracts/artifact_contract.md) — Artifact Contract.
- [architecture/contracts/backup_restore_contract.md](architecture/contracts/backup_restore_contract.md) — Backup/Restore Contract.
- [architecture/contracts/core_worker_protocol_contract.md](architecture/contracts/core_worker_protocol_contract.md) — CORE ↔ Worker Protocol Contract.
- [architecture/contracts/gpu_provider_contract.md](architecture/contracts/gpu_provider_contract.md) — GPU Provider Contract.
- [architecture/contracts/module_contract.md](architecture/contracts/module_contract.md) — Module Contract.
- [architecture/contracts/workflow_dag_contract.md](architecture/contracts/workflow_dag_contract.md) — Workflow DAG Contract.
- [architecture/contracts-review-367.md](architecture/contracts-review-367.md) — Review контрактів Architecture 2.0 — #367.

## Приймання та докази

- [acceptance/removal-446.md](acceptance/removal-446.md) — Реалізація й межі приймання видалення #446.
- [acceptance/agents-docs-448.md](acceptance/agents-docs-448.md) — Реорганізація правил і документації — #448.
- [acceptance/core-access-acceptance.md](acceptance/core-access-acceptance.md) — CORE access: приймання #362 (i.0.0.3.62).
- [acceptance/execution-acceptance-368.md](acceptance/execution-acceptance-368.md) — Architecture 2.0 — локальне приймання #367 / #368.
- [acceptance/execution-acceptance-369-370.md](acceptance/execution-acceptance-369-370.md) — Локальна реалізація #369 і #370.
- [acceptance/health-and-role-activation.md](acceptance/health-and-role-activation.md) — Health і застосування ролей — #449 та #450.
- [acceptance/mpt-composition-acceptance.md](acceptance/mpt-composition-acceptance.md) — Перевірка #365 та #366.
- [acceptance/persistence-trust-acceptance.md](acceptance/persistence-trust-acceptance.md) — Приймання #354 та #355.
- [acceptance/provider-node-acceptance.md](acceptance/provider-node-acceptance.md) — Локальна перевірка #363 / #364.
- [acceptance/README.md](acceptance/README.md) — Приймання та докази.
- [acceptance/surface-acceptance.md](acceptance/surface-acceptance.md) — Автоматизоване приймання #356 та #357.
- [acceptance/text-redis-acceptance.md](acceptance/text-redis-acceptance.md) — Text / Redis acceptance — #358, #359.
- [acceptance/voice-publisher-acceptance.md](acceptance/voice-publisher-acceptance.md) — Voice / Publisher acceptance — #360, #361.
- [acceptance/web-gpu-acceptance.md](acceptance/web-gpu-acceptance.md) — Приймання #352 та #353.

## Процедури LAB

- [lab/lab-gpu-contract.md](lab/lab-gpu-contract.md) — GPU Idle-Lease Contract між лабораторією та Vertep Production.
- [lab/lab-issue-cycle.md](lab/lab-issue-cycle.md) — LAB: наскрізний цикл Issues (var.0.0.3.98, #398).
- [lab/lab-qualification.md](lab/lab-qualification.md) — Кваліфікація лабораторії — докази R1–R6 (Issue #252, `ir.0.0.2.52`).
- [lab/operations.md](lab/operations.md) — LAB: операції та діагностика.
- [lab/README.md](lab/README.md) — Процедури лабораторії.

## Історичні матеріали

- [archive/agents-reference-before-448.md](archive/agents-reference-before-448.md) — Довідкові розділи AGENTS.md до #448.
- [archive/deployment-security-audit.md](archive/deployment-security-audit.md) — Deployment Wizard: technical and security audit.
- [archive/lab-checklist-etap4.md](archive/lab-checklist-etap4.md) — Чекліст Етапу 4 — GPU Idle-Lease Інтеграція.
- [archive/lab-checklist-etap5.md](archive/lab-checklist-etap5.md) — Чекліст Етапу 5 — Docker-розгортання та Systemd.
- [archive/lab-checklist-etap6.md](archive/lab-checklist-etap6.md) — Чекліст Етапу 6 — Перший повноцінний аудит.
- [archive/lab-checklist-etap7.md](archive/lab-checklist-etap7.md) — Чекліст Етапу 7 — Повний 24/7 Режим.
- [archive/lab-overview-before-448.md](archive/lab-overview-before-448.md) — Огляд LAB до реорганізації #448.
- [archive/large-files-refactoring.md](archive/large-files-refactoring.md) — Рефакторинг великих файлів Vertep CORE.
- [archive/open-source-audit.md](archive/open-source-audit.md) — Технічний аудит open-source рішень та оптимізація архітектури Vertep.
- [archive/README.md](archive/README.md) — Історичні матеріали.
- [archive/requirements-gap-analysis.md](archive/requirements-gap-analysis.md) — Аналіз прогалин трьох технічних завдань Vertep.

## Конвенції

- Чинні настанови — у корені; контракти — в `architecture/contracts/`; процедури й звіти приймання — в `acceptance/`; LAB — у `lab/`; історичні знімки — в `archive/`.
- Документ має статус, аудиторію та посилання на нормативний канон. «Чинний опис» не означає production-кваліфікацію; результати й межі перевірки вказуються окремо.
- Відносні Markdown-посилання мають вести на наявні файли. Новий документ додається до цього індексу; при переміщенні оновлюються актуальні посилання. Шляхи в історичних release notes та знімках не переписуються.
- Заборонені паралельні roadmap/TODO/backlog; індекс упорядковує лише документи. Повноваження щодо GitHub, production, push/release не виникають із прочитаної інструкції.
