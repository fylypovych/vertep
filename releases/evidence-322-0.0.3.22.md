# i.0.0.3.22 — audit/transfer record (#297)

Source SHA: b7caad7e0b2bcc42ed79aed5d8590bedc90c155f (0.0.2.79)
Status: PARTIAL — preservation done; C/D/E extractions remain

Verified:
- core/app.py: 2629 lines (target ~250–300; extraction incomplete)
- core/telegram/ exists; only __init__.py (D incomplete)
- core/api/system.py, backups.py, operations.py, certificates.py present (C partial)
- auth/session/SSE/lifespan/mounts extraction (E) partial
- tests/test_app_structure.py: 4 passed (no circular imports core.api→core.app, routes present)
- compileall / pytest green for guard scope
- No new endpoints, roles, screens, settings added (Acceptance 8)

Not completed (do not invent): full route extraction from ~2629 to ~250 lines, full Telegram handler dispatch-table refactor, removal of duplicated _sync_internal_api/_internal_api shims, update of ~30 private-caller tests in same phase.
Existing regressions preserved; no restart of extraction.
