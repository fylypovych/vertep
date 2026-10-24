# i.0.0.3.14 — audit/transfer record (#289 #163.2–#164.3)

Status: PARTIAL — default/retry/history tests added (core/storyboard.py); old revision fix preserved.
Verified: test_storyboard.py exists; core/storyboard pins None/default.
Remaining (not invented):
- #163.2 actual persistence/restart/recovery of current revision
- #163.3 heartbeat/lease-loss/dead-letter consistency (Job/Worker/repo/queue ownership)
- #164.1 readiness/model routing in controlled Text harness
- #164.3 link missing default/recovery/LLM to shared ownership/media with exact SHA
No new functionality; no new endpoints.
