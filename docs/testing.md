# Тестування

> Статус: чинний | Аудиторія: розробник | Канон: `AGENTS.md` §21, §3.9, §3.12

## Обов'язкові перевірки (перед кожним версійним commit)

```bash
python -m compileall -q core adapters worker scripts installer tests
python -m pytest -q
git diff --check
```

Плюс: відсутність нових падінь тестів, секретів/ключів у дифі, узгодженість `VERSION = commit = CHANGELOG = releases/<версія>.md`.

## Рівні

- **Unit/contract/integration** (`tests/`) — локально, без живого сервера: API, tasks, queue semantics, worker contracts, control-plane межі, backup/restore на disposable-сховищах.
- **Browser E2E** (Playwright, `web-v2/tests`, `tests/test_browser_e2e.py`) — критичні flows зі справжнім локальним backend і реальними тестовими артефактами; route-mock тести валідні лише для UI, не для backend-контрактів.
- **Qualification-скрипти** — `scripts/qualify-release.py` (артефакти релізу), `scripts/qualify_infrastructure.py` (S01–S08, JSON-доказ).
- **Real Test** (`ir` Issues) — фактичний стенд: clean Ubuntu, multi-host, physical GPU, live platforms; mock/skipped/green unit — не доказ.

## Політика

- Нові падіння модульних тестів блокують реліз; допустимі e2e-невдачі — лише відсутність запущеного сервера (`ERR_CONNECTION_REFUSED 127.0.0.1:8080`).
- Skipped ≠ passed: пропущені перевірки (OpenSSL, GPU, live) фіксуються явно і не зараховуються.
- Release gate: required Browser E2E для точного release SHA має бути green — інакше публікація блокується (§3.12).

## CI

- `ci.yml` — повний suite, компіляція, shell/compose-перевірки на кожен push/PR.
- `browser-e2e.yml` — Browser suite з артефактами (logs/traces).
- `release.yml` — build images, runtime bundle, manifest/checksums/SBOM/підпис, валідація, tag + GitHub Release. Єдиний власник публікації.
