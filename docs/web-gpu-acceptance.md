# Приймання #352 та #353

Документ визначає трасування перевірок, а не оголошує непроведені перевірки PASS.
Фактичні результати, повний Vertep SHA та dirty/clean статус містяться в JUnit.

| Критерій | Реалізація / test ID | Expected |
|---|---|---|
| #352 AC1 | `test_web_state_browser_e2e::test_read_loading_failure_retry_without_mutation` | Jobs/Queue/Fleet/Dashboard/Settings/Alerts/Logs/Health: затриманий справжній HTTP → loading; network failure → error; retry → справжній API; write-запитів немає. |
| #352 AC1, Channels | `test_channels_read_retry_mutation_and_permission` | Read failure не перетворюється на empty. Retry повертає actual empty; create/toggle змінюють persisted channel; EMERGENCY блокує UI та API mutation, дані незмінні. |
| #352 AC2 | `test_routes_localized_dates_and_responsive_layout`, `test_keyboard_modal_focus_cancel_has_no_mutation` | Чинні route titles, український формат дати, widths 1280/390 без overflow; keyboard Enter/Tab/Shift+Tab/Escape, focus trap/return, контраст confirm button ≥4.5 та cancel без мутації. |
| #352 AC2, чинний smoke | `test_existing_route_localization_and_runner_smoke` | Повторно використовує чотири чинні header/localization/collapsed-sidebar/Runner UI smoke tests. Їхні mocked payloads не зараховуються як actual Runner proof. |
| #352 AC3 | `test_media_review_acceptance.py`, `test_storyboard.py`, `test_image_storyboard_e2e.py`, `test_telegram_approval_flow.py` | HTTP Resume/Retry → NEW не обходить missing/mismatched/unapproved video storyboard; stale approvals повертають 409 без зміни persisted JSON; чинні version/revision guards та Telegram callbacks збережені. |
| #352 AC3, AC5 | `test_media_browser_e2e.py` | Actual Browser creation/script review, storyboard history, scene/all revisions, regenerate, stale409, video review та publication зі збереженими результатами. Лише зовнішні provider transports контрольовані. |
| #352 AC4, AC5 | `test_runner_browser_e2e::test_runner_web_actual_api_progress_history_restart_and_retry` | Actual prerequisites/confirm/run/progress/history/report, authenticated initiator, restart CORE process; report retries не створюють новий run і не повторюють completed checks. Runner API не підміняється. |
| #353 AC1 | `test_gpu_built_inventory::test_built_inventory_delivery_readiness_dispatch_and_media_trace` | Build незміненого `docker/comfyui-nvidia/Dockerfile`; image content digest; report із image звірено з actual git checkouts. Production entrypoint доставляє report; bundle pins → standard deployment generator → expected inventory → Worker readiness. |
| #353 AC2 | Той самий test ID | Missing inventory, component drift, workflow checksum mismatch дають FAILED self-test і блокують actual task claim; відновлення inventory повертає PASSED та дозволяє claim. |
| #353 AC3 | Той самий test ID + `test_gpu_http_pipeline.py`, `test_gpu_media_fixtures.py` | Identity того самого image/inventory прив’язана до CORE→Worker→prompt/history/view→result/import; actual persisted MP4 bytes, SHA256 та artifact references. Чинні decoder/fault tests повторно використані. |

Для #353 обов’язковий Docker Linux engine. Запуск:

```powershell
$env:VERTEP_REQUIRE_GPU_BUILT_INVENTORY = "1"
python -m pytest -q tests/test_gpu_built_inventory.py tests/test_gpu_runtime_inventory_delivery.py tests/test_gpu_http_pipeline.py tests/test_gpu_media_fixtures.py tests/test_comfyui_runtime_inventory.py -o junit_family=legacy --junitxml=.pytest_tmp/gpu-inventory.xml
```

Image збирається з чинного Dockerfile без перевизначення pins. `--cpu --help`
дозволяє перевірити entrypoint delivery без фізичного GPU. Негативні inventory
сценарії змінюють лише disposable expected files. Rendering використовує вже
погоджений controlled HTTP provider, workflow/model bytes — synthetic fixtures;
це не доказ physical GPU inference (чинний власник — ir #34).

Без required flag built-image test має skip/BLOCKED; відсутність Docker з
увімкненим flag завершує перевірку помилкою. Helper-generated report не
використовується для позитивного image proof. FFmpeg skips не є FFmpeg PASS.

CI jobs `browser-e2e` та `gpu-built-inventory` зберігають JUnit, повний SHA,
tracked diff/test hashes, process/build logs та API/image/media evidence.
Dirty локальний запуск не є підтвердженням незміненого commit у main.
Legacy v1 не видалено; parity перевіряється лише в погодженому scope цих flows.
