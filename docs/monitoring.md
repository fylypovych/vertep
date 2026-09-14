# Моніторинг

> Статус: чинний | Аудиторія: оператор | Канон: `AGENTS.md` §7 (роль monitoring)

## Стек

Monitoring Node: Prometheus + Grafana + Loki (+ Promtail/collector), persistent volumes. Конфігурація (scrape, dashboards, rules) — у digest-pinned образах, не в мутабельних runtime-файлах.

## Що спостерігається

- Health probes: PostgreSQL, Redis, API, Web UI, Ollama, Dispatcher, GPU/Driver/CUDA, ComfyUI, VRAM (`/api/health`, readiness для контейнерних health gate).
- Структуровані ротовані логи з cursor/pagination, retention і посиланнями на Job/Node.
- Alerts як persistent-сутності: stable ID, firing/resolved lifecycle, acknowledge (actor/time), dedup, retention; recovery не стирає історію.

## Сценарій приймання

Зупинка Worker/service → деградація проб → alert/log у UI → перехід до Node/Job → acknowledge → recovery → resolved; історія переживає рестарт.

## Де дивитися

Web UI: Alerts / Logs / Health; метрики Jobs/Fleet/Dashboard узгоджені з backend. Недоступні дані показуються як unavailable/error, ніколи як фальшивий `0`.
