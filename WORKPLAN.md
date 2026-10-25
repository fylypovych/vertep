# Work Plan for Issues #344 and #345

## Current State
- VERSION: 0.0.2.81
- Modified files: core/integration/architecture_20.py, core/workflow/engine.py
- Tests: 18/18 MPT recovery tests pass, 9/9 core generation gate tests pass

## Issue #344: i.0.0.3.44 — MPT: pinned upstream matrix та independent recovery proof
### Status: PARTIAL (audit 24.10.2026)
### Source: #320 (i.0.0.3.20)
### Tests Status:
- test_separate_core_and_worker_have_different_storage_roots: PASSED
- test_engine_restart_via_reinstantiation_recovers_approved_voice: PASSED
- All 18 MPT recovery tests: PASSED

### Acceptance Criteria:
- Completed subdivisions clearly separated in audit
- Complete only verified remainder with criterion→test→expected/actual on exact SHA
- No new requirements
- Uncommitted changes not lost, reported before work
- Push/release/branch and new rights not allowed

### Action: Verify implementation is complete and correct. Tests already pass, need to ensure changes are properly committed to main with correct version.

## Issue #345: i.0.0.3.45 — CORE composition root: відновити routes і завершити extraction guards
### Status: PARTIAL/FAIL (audit 24.10.2026)
### Source: #322 (i.0.0.3.22)
### Current: core/app.py has 2868 lines

### Acceptance Criteria:
- **Acceptance 1**: API (method, path) set before and after must be the same - confirmed by test, not manual check; only route code changes, not their contracts
- **Acceptance 3**: core/app.py does not contain business logic and endpoints; target ~250-300 lines
- **Acceptance 4**: No module in core/api/ and core/telegram/ imports core.app
- **Acceptance 5**: tests/test_core_generation_gate.py green on every phase
- **Acceptance 6**: Tests accessing private _handle_* through core.app updated within same phase during relocation (~30 points)
- **Acceptance 7**: Greens: python -m compileall -q, pytest -q, git diff --check
- **Acceptance 8**: No new function, endpoint, parameter, role, screen or setting; commits not accompanied by new product requirements

### Extraction Sections (A-F):
- **A**: Extract ~580 lines (2190-2772) from core/app.py into core/api/system.py, backups.py, operations.py, certificates.py
- **B**: Extract system API routes
- **C**: Extract auth/session/SSE, lifespan and web UI mounts (~380 lines, sections 156-507, 103-152, 2773-2804)
- **D**: Extract specific modules
- **E**: Extract auth/session/SSE, lifespan and web UI mounts (different section)
- **F**: Guard-rail tests

### Current Modified Files (partial work):
- core/integration/architecture_20.py: Changed dispatcher import (from core.dispatcher.dispatcher → core.dispatcher)
- core/workflow/engine.py: Added _load_existing_jobs() method and job persistence with dependencies/type fields

### Plan Phase 1: Issue #345 Implementation
#### Step 1: Analyze core/app.py sections to extract
#### Step 2: Create core/api/system.py with extracted system API code
#### Step 3: Create core/api/backups.py with extracted backup code
#### Step 4: Create core/api/operations.py with extracted operations code
#### Step 5: Create core/api/certificates.py with extracted certificates code
#### Step 6: Update core/app.py to import from new modules instead of containing the code
#### Step 7: Update all imports throughout the codebase
#### Step 8: Update tests that access _handle_* through core.app
#### Step 9: Verify all acceptance criteria pass
#### Step 10: Run full test suite

### Workflow (§4.8): URL Issue → аналіз вимог → аудит коду → планування → реалізація → тестування → звіт користувачу → STOP
### Constraint: No push/release without explicit user command. After reporting status, will STOP.
