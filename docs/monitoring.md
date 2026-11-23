# Моніторинг

> Статус: чинний | Аудиторія: оператор | Канон: [AGENTS.md](../AGENTS.md) §15.

## Модуль Monitoring (#503)

Необов'язковий Application Module (`modules/monitoring`): адміністратор встановлює його в **Модулі** й розміщує на CORE або іншому вузлі (`placement.target: any`). Історія метрик і подій — у власній schema модуля в PostgreSQL; типовий строк зберігання — 3 дні з автоматичною ротацією, адміністратор може лише збільшити його (`modules/monitoring/monitoring_logic.py::retention_days`). Перед видаленням модуля історія архівується, архів відновлюється тим самим lifecycle.

## Стек ролі сумісності

Legacy-роль Monitoring Node: Prometheus + Grafana + Loki (+ Promtail/collector), persistent volumes. Конфігурація (scrape, dashboards, rules) — у digest-pinned образах, не в мутабельних runtime-файлах. Роль лишається для сумісності й мігрує під керування модуля.

## Що спостерігається

- Health probes: PostgreSQL, Redis, API, Web UI, Ollama, Dispatcher, GPU/Driver/CUDA, ComfyUI, VRAM (`/api/health`, readiness для контейнерних health gate).
- Структуровані ротовані логи з cursor/pagination, retention і посиланнями на Job/Node.
- Alerts як persistent-сутності: stable ID, firing/resolved lifecycle, acknowledge (actor/time), dedup, retention; recovery не стирає історію.

## Сценарій приймання

Зупинка Worker/service → деградація проб → alert/log у UI → перехід до Node/Job → acknowledge → recovery → resolved; історія переживає рестарт.

## Де дивитися

Web UI: Alerts / Logs / Health; метрики Jobs/Fleet/Dashboard узгоджені з backend. Недоступні дані показуються як unavailable/error, ніколи як фальшивий `0`.
