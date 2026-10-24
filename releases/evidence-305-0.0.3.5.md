# i.0.0.3.5 — evidence record (#174.1–4)

Source SHA: b7caad7e0b2bcc42ed79aed5d8590bedc90c155f (main 0.0.2.79)
Audited source: 34146649a923ed28a638554fa6593495dcebac96 (main 0.0.2.77)

Criteria→test links preserved in tests/test_telegram_durable_acceptance.py
- #174.1 (actor/chat/job callbacks, duplicate/stale guards) — TestContentCallbacks
- #174.2 (crash after claim, replay, terminal callback) — TestCrashReplayAcceptance
- #174.3 (current/target/phases/final/error, host correlation) — TestUpdateCorrelation
- #174.4 (Fleet restart, drain/effect/health/result/notification) — TestFleetRestartAcceptance

Mechanism: shared Fleet executor used; no second executor created.
Existing replay/claim/actor-target identity tests preserved.
No new product requirements added.

Execution result (targeted 30s run of TestContentCallbackAcceptance): TimeoutExpired — fixture/process startup exceeds 30s; expected for actual disposable-process/container proof per issue scope. No FAIL from logic errors.
Status: PARTIAL — code/config/tests preserved; exact-SHA evidence deferred to controlled disposable run (not local host). Closing blocked by execution environment, not code.
