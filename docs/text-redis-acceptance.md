# Text / Redis acceptance — #358, #359

## Межі доказу

`tests/test_queue_redis_contract.py` запускає disposable Redis та перевіряє
AOF/RDB persistence, cross-client читання й узгодженість Queue / Dashboard / metrics.
`tests/test_text_redis_process.py` запускає окремі процеси Redis і CORE (uvicorn),
виконує claim/result/renew/cancel через HTTP, перезапускає CORE та аварійно завершує
Redis перед відновленням AOF. Local queue fallback не приймається як доказ.
Тільки зовнішній Ollama HTTP transport контрольований; worker виконує штатний
`execute_role_task("text", task)`. Це не перевірка якості реальної LLM.

Локальне середовище: Windows, portable Redis 8.10.3 (`redis-windows/redis-windows`,
release `8.10.3`, asset `Redis-8.10.3-Windows-x64-msys2.zip`). SHA256 архіву:
`af16df340525b431e9ec8dabfd625d5e7a233f82805b5418819904ea909c447f`.
Evidence містить фактичні Redis version і SHA256 binary. Ubuntu CI використовує
встановлений `redis-server`; його відсутність завершує процесні тести помилкою.

## Відтворені причини

- Старі Redis API-тести підміняли queue лише в `core.api.tasks`; Dashboard і metrics
  продовжували читати shared local queue. Fixture тепер змінює backend shared object.
- Queue summary відкидав dead-letter `error`; також очікування job-2/job-3 не
  відповідало фактичному порядку priority claim. Очікування прив'язано до task IDs.
- Після restart CORE старий script lease залишався inflight поруч із новим task.
- Heartbeat-loss та storyboard lease recovery залишали superseded task у ready.
  Script lease expiry не переводив Job з GENERATING назад у чергу.
- Storyboard lease recovery підставляв попередній `revision_request` замість
  pinned default `None`. History branch після невдалої незбереженої версії
  успадковував вичерпаний бюджет.
- Text claim не записував owner у Job; cancel не доставляв запит власнику.
  Worker ownership після heartbeat-loss не зберігався на диск.
- Terminal storyboard failure повертався до cleanup lease/owner; terminal script
  failure двічі додавав той самий task у dead-letter. Accepted failure завершує
  lease та залишає один запис на task.

## Матриця

| Перевірка | Доказ |
| --- | --- |
| Shared Redis backend, ready/inflight/dead-letter counts, task IDs | `test_counts_state_consistent_across_restart_and_worker_loss`, `test_dashboard_jobs_queue_http_consistency_through_loss_and_core_restart` |
| Реальні restart CORE / Redis crash, AOF, stale result fence | `test_core_and_redis_restart_preserve_unique_text_tasks` |
| Default / explicit revision, retry, dead-letter, restart, history branch | `test_storyboard_revision_retry_loss_restart_uses_current_default[heartbeat]`, `[lease]` |
| Claim owner, lease renew, cancel delivery, stale result, repository | `test_text_cancel_fences_result_and_releases_queue` |
| Script heartbeat-loss / lease expiry, один replacement, persisted owner | `test_script_loss_has_one_replacement_and_no_phantom_owner[heartbeat]`, `[lease]` |
| Failed result, bounded retry, terminal state, унікальні dead-letter IDs | `test_failed_text_results_exhaust_budget_without_duplicate_dead_letters[script]`, `[storyboard]` |

Процесні тести записують `text_process_*` у той самий evidence JSON та run_id,
що й shared media harness. CI запускає їх одним pytest invocation і перевіряє
`media-acceptance-report.py --require-all --include-text-process --expected-sha "$GITHUB_SHA"`.
Відсутність процесного рядка блокує PASS. JUnit додатково фіксує source SHA,
hash робочого diff і hash тесту. Локальне dirty-tree виконання не замінює green CI
для майбутнього опублікованого SHA.
