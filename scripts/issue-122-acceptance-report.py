#!/usr/bin/env python3
"""Issue #122 P10 — єдиний трасований acceptance-звіт за матрицею §6.

§6 вимагає не «зеленого CI», а оцінки кожного критерію окремо: точний SHA, pinned
upstream/digest, config revision, ім'я тесту, expected/actual і артефактні хеші, причому
``skipped``/``missing`` **не є** PASS. Цей скрипт виконує саме це:

* кожен рядок §6 оголошено як критерій з власним owner-Issue (§8) і списком тестів, які
  є його автоматизованим доказом;
* рядок отримує ``PASS`` лише коли **всі** його потреби доступні в цьому середовищі
  (``runtime``/``browser``/``stand``) **і** кожен задекларований тест зібрано, виконано й
  пройдено; інакше ``NOT_RUN`` з причиною або ``FAIL``;
* невідома тестова id або тест, який не був виконаний, робить рядок ``FAIL``, а не тихою
  відсутністю;
* звіт пишеться у JSON і Markdown разом із SHA робочого дерева, dirty-відміткою,
  конфігураційною ревізією, pinned upstream та образом digest, і завершується ненульовим
  кодом, якщо є ``FAIL`` (а з ``--require-all`` — і якщо є ``NOT_RUN``).

Скрипт нічого не публікує і нічого не встановлює: він лише збирає докази й чесно каже,
які рядки §6 у цьому середовищі довести неможливо.

Використання::

    python scripts/issue-122-acceptance-report.py
    python scripts/issue-122-acceptance-report.py --out reports/issue-122 --require-all
    python scripts/issue-122-acceptance-report.py --only contract,recovery
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.providers.base import BRIDGE_SCHEMA_VERSION  # noqa: E402
from adapters.providers.runtime_manifest import (  # noqa: E402
    PINNED_UPSTREAM_COMMIT,
    PINNED_UPSTREAM_REFERENCE,
)

#: Можливі статуси рядка. ``NOT_RUN`` ніколи не рахується як PASS (§6 Evidence).
PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"

#: Evidence artefacts of the pinned #389 M1–M4 run (#392 M5). They are produced by the
#: existing writers (``moneyprinter-runtime-check.py``, ``moneyprinter-recovery-check.py``
#: and the pinned pytest matrix), never by a second evidence system.
PINNED_SNAPSHOT = "runtime-snapshot.json"
PINNED_RECOVERY = "recovery.json"
PINNED_MEDIA_MATRIX = "media-matrix-pinned.xml"
PINNED_ARTEFACTS = (PINNED_SNAPSHOT, PINNED_RECOVERY, PINNED_MEDIA_MATRIX)
#: #397 M2: separate CORE and Worker processes on the pinned runtime. Optional in a bundle;
#: without it M2 stays NOT_RUN.
PINNED_PROCESS_LIFECYCLE = "process-lifecycle-pinned.xml"
PROCESS_LIFECYCLE_TESTS = (
    "tests/test_mpt_process_lifecycle.py"
    "::test_core_dispatches_and_a_separate_worker_process_renders_two_revisions",
    "tests/test_mpt_process_lifecycle.py"
    "::test_native_engine_regression_renders_in_core_without_a_worker[native]",
)
#: #397 M3: a node killed mid-render loses its lease; the attempt is reassigned and the
#: runtime reconciles the same submit key. Recorded in the same process-lifecycle JUnit.
M3_PROCESS_LEASE_TEST = (
    "tests/test_mpt_process_lifecycle.py"
    "::test_a_node_killed_mid_render_loses_its_lease_and_another_node_finishes_the_attempt"
)

#: #392 moved the corrupt-download fault from the M1 media matrix to M3 recovery, so the
#: two criteria partition the matrix instead of both claiming (or both dropping) it.
CORRUPT_DOWNLOAD_TEST = (
    "tests/test_mpt_media_matrix.py::test_a_corrupted_download_is_never_registered"
)
VOICE_STAGING_NEGATIVE_TEST = (
    "tests/test_mpt_media_matrix.py"
    "::test_a_runtime_without_voice_staging_is_refused_before_dispatch"
)

#: Assertions ``moneyprinter-recovery-check.py`` must actually record for #389 M3. The
#: writer names each one; prose about "recovery" in general is not evidence for a
#: specific scenario, so an absent assertion keeps the criterion NOT_RUN.
M3_REQUIRED_ASSERTIONS = (
    "a repeated submit key is reconciled",
    "the retry created no upstream task",
    "the key reconciles to submitted after a runtime restart",
    "a retry after the restart still creates no second task",
)

#: "busy DELETE answers 409" is a distinct M3 scenario: a 404 on an absent task and a
#: refusal that only accepts 409 *or* 404 are not that proof, so the bundle has to say
#: 409 explicitly in a passed assertion.
M3_BUSY_CANCEL_MARKER = "409"

#: The §389 source criteria the pinned bundle answers (#392 M1–M4).
PINNED_CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "id": "M1",
        "title": "Pinned media matrix: agreed §9.3 timeline через реальний "
                 "submit/status/download, зокрема capability-negative voice staging",
        "owner": "#389 M1 / #122 §9",
        "artefacts": (PINNED_MEDIA_MATRIX, PINNED_SNAPSHOT),
        "tests": (VOICE_STAGING_NEGATIVE_TEST,),
    },
    {
        "id": "M2",
        "title": "Окремі CORE/Worker processes: штатний Job/approval/queue/claim → pinned "
                 "MPT → validated import, різні storage roots",
        "owner": "#389 M2 / #180.1–3",
        "artefacts": (PINNED_PROCESS_LIFECYCLE,),
        "tests": PROCESS_LIFECYCLE_TESTS,
    },
    {
        "id": "M3",
        "title": "Recovery: submit/lost response, restarts, lease fencing, bounded polling, "
                 "busy 409, corrupt download, duplicate/late result",
        "owner": "#389 M3 / #180.6–9",
        "artefacts": (PINNED_RECOVERY, PINNED_MEDIA_MATRIX),
        "tests": (CORRUPT_DOWNLOAD_TEST,),
    },
    {
        "id": "M4",
        "title": "Restart у вікні acceptance→staging і cleanup/duplicate key без втрати "
                 "approved inputs",
        "owner": "#389 M4 / #179.1–4",
        "artefacts": (PINNED_RECOVERY,),
        "tests": (),
    },
)

#: Середовищні можливості, яких може бракувати. Рядок, який їх потребує, у середовищі без
#: них не отримує PASS навіть із зеленими тестами.
RUNTIME = "pinned_runtime_container"
BROWSER = "live_server_with_browser"
STAND = "authorised_real_stand"

_ACCEPTANCE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": "contract",
        "title": "Обидва engines, schema versions, supported/unsupported, auth, "
                 "status mapping, відсутній output",
        "owner": "#122 §6 Contract",
        "needs": (),
        "tests": (
            "tests/test_video_engines.py::test_submit_matches_the_fixed_bridge_contract",
            "tests/test_video_engines.py::test_submit_sends_every_fixed_submission_value",
            "tests/test_moneyprinter_runtime.py::test_the_self_test_submits_the_very_request_the_engine_sends",
            "tests/test_moneyprinter_runtime.py::test_the_runtime_gate_refuses_a_schema_that_dropped_a_fixed_submit_field",
            "tests/test_video_engines.py::test_build_upstream_request_matches_pinned_schema",
            "tests/test_video_engines.py::test_build_upstream_request_requires_pinned_profile",
            "tests/test_video_engines.py::test_remote_engine_contract_negative_cases",
            "tests/test_video_engines.py::test_every_declared_rejection_code_has_a_negative_case",
            "tests/test_video_engines.py::test_map_upstream_status_states",
            "tests/test_video_engines.py::test_map_upstream_status_rejects_malformed",
            "tests/test_video_engines.py::test_remote_engine_sends_bearer_token",
            "tests/test_video_engines.py::test_health_check_refuses_when_auth_is_not_proven",
            "tests/test_video_engines.py::test_native_engine_renders_real_video",
            "tests/test_video_engines.py::test_remote_engine_http_flow",
            "tests/test_moneyprinter_runtime.py::test_wrapper_health_reports_ready_only_with_auth_and_schema_proof",
        ),
    },
    {
        "id": "media",
        "title": "Декодовані fixtures, порядок сцен, durations, aspect/preset, audio, "
                 "SRT/BGM/watermark за погодженою matrix",
        "owner": "#122 §6 Media",
        "needs": (),
        "tests": (
            "tests/test_video_engines.py::test_engine_parity_native_vs_external_on_same_job",
            "tests/test_video_engines.py::test_clip_duration_never_exceeds_the_upstream_limit",
            "tests/test_video_engines.py::test_moneyprinter_refuses_a_download_that_is_not_decodable_media",
            "tests/test_video_engines.py::test_verified_import_is_readable_and_checksummed",
            "tests/test_video_engines.py::test_material_upload_reads_the_real_upstream_envelope",
            "tests/test_video_engines.py::test_parse_output_reference_normalises_task_scoped_paths",
            "tests/test_moneyprinter_runtime.py::test_wrapper_media_proof_refuses_a_wrong_scene_timeline",
            "tests/test_moneyprinter_runtime.py::test_runtime_check_requires_a_proven_scene_timeline",
            "tests/test_moneyprinter_runtime.py::test_the_probe_clip_meets_the_minimum_the_pinned_runtime_enforces",
            "tests/test_moneyprinter_runtime.py::test_the_probe_clip_is_encoded_with_the_verified_geometry",
            "tests/test_moneyprinter_runtime.py::test_a_probe_below_the_pinned_minimum_is_refused_before_the_upstream",
        ),
    },
    {
        "id": "integration",
        "title": "Штатні queue/claim/result/approval/import шляхи; один task owner",
        "owner": "#122 §6 Integration, #104",
        "needs": (),
        "tests": (
            "tests/test_assembly_worker_route.py::test_external_engine_dispatches_to_a_worker_without_rendering_in_core",
            "tests/test_assembly_worker_route.py::test_a_separate_worker_process_renders_the_dispatched_attempt",
            "tests/test_assembly_worker_route.py::test_a_worker_claims_the_attempt_and_fences_the_result",
            "tests/test_assembly_worker_route.py::test_worker_renders_the_dispatched_version_and_returns_a_verifiable_contract",
            "tests/test_assembly_worker_route.py::test_a_matching_node_is_handed_the_attempt",
            "tests/test_assembly_worker_route.py::test_an_external_render_cannot_be_published_before_approval",
            "tests/test_assembly_worker_route.py::test_only_the_current_approved_artifact_is_publishable",
            "tests/test_assembly_worker_route.py::test_two_revision_loops_keep_every_accepted_version_immutable",
        ),
    },
    {
        "id": "recovery",
        "title": "Усі рядки §5: submit/lost response, restart, lease, timeout/5xx, cancel, "
                 "409, обірваний download, duplicate/late result, switch під час Job",
        "owner": "#122 §5, §6 Recovery",
        "needs": (),
        "tests": (
            "tests/test_video_engines.py::test_render_sends_the_durable_submit_key_and_uses_the_reconciled_task",
            "tests/test_video_engines.py::test_a_lost_submit_that_cannot_be_reconciled_is_terminal",
            "tests/test_video_engines.py::test_an_in_flight_submit_is_reconciled_instead_of_repeated",
            "tests/test_video_engines.py::test_an_interrupted_download_is_retried_under_the_same_submit_key",
            "tests/test_video_engines.py::test_a_download_transferred_encoded_is_not_judged_by_its_wire_length",
            "tests/test_video_engines.py::test_moneyprinter_bounds_polling_retries_and_refuses_the_task",
            "tests/test_video_engines.py::test_a_rate_limited_status_is_retried_within_a_bound_and_ends_terminally",
            "tests/test_video_engines.py::test_a_malformed_status_body_is_a_clear_terminal_failure",
            "tests/test_video_engines.py::test_remote_engine_cancel_treats_busy_task_as_not_cancelled",
            "tests/test_video_engines.py::test_a_still_running_runtime_is_never_released",
            "tests/test_video_engines.py::test_an_unanswering_runtime_is_never_released",
            "tests/test_video_engines.py::test_a_task_the_upstream_no_longer_knows_reports_absent",
            "tests/test_video_engines.py::test_an_unreachable_upstream_is_unreachable_and_never_absent",
            "tests/test_video_engines.py::test_a_local_render_holds_no_remote_runtime",
            "tests/test_assembly_worker_route.py::test_a_restart_drops_the_stale_attempt_and_replays_the_same_key",
            "tests/test_assembly_worker_route.py::test_cancellation_fences_the_attempt_and_refuses_a_late_result",
            "tests/test_assembly_worker_route.py::test_a_refused_abort_is_recorded_as_an_unconfirmed_release",
            "tests/test_assembly_worker_route.py::test_a_runtime_whose_task_is_gone_is_recorded_as_released",
            "tests/test_assembly_worker_route.py::test_an_accepted_abort_is_recorded_as_released",
            "tests/test_assembly_worker_route.py::test_an_unreachable_runtime_never_reports_a_release",
            "tests/test_assembly_worker_route.py::test_a_lease_is_not_handed_to_another_job_while_the_release_is_unproven",
            "tests/test_assembly_worker_route.py::test_a_release_of_another_runtime_does_not_hold_this_one",
            "tests/test_assembly_worker_route.py::test_the_hold_is_released_when_the_late_render_proves_the_release",
            "tests/test_assembly_worker_route.py::test_a_late_result_of_a_superseded_attempt_cannot_overwrite_the_accepted_version",
            "tests/test_assembly_worker_route.py::test_a_superseded_attempt_is_not_offered_to_a_worker",
            "tests/test_moneyprinter_runtime.py::test_wrapper_records_the_submit_key_before_contacting_the_upstream",
            "tests/test_moneyprinter_runtime.py::test_wrapper_reconciles_a_lost_response_through_the_submit_key",
            "tests/test_moneyprinter_runtime.py::test_a_repeat_submit_after_a_restart_completes_the_voice_staging",
            "tests/test_moneyprinter_runtime.py::test_a_corrupted_submit_record_is_never_treated_as_a_new_submit",
            "tests/test_moneyprinter_runtime.py::test_a_definitively_refused_submit_releases_its_key_for_a_retry",
            "tests/test_moneyprinter_runtime.py::test_wrapper_aborts_an_attempt_by_its_durable_submit_key",
        ),
    },
    {
        "id": "lifecycle_state",
        "title": "Disposable lifecycle state: optional runtime install, backup/restore of "
                 "engine selection/endpoint/secret references, mappings, artifacts/hashes, "
                 "history and approvals without a container",
        "owner": "#122 §6 Lifecycle, #129 (#34/#35/#42)",
        "needs": (),
        "tests": (
            "tests/test_moneyprinter_runtime.py::test_a_backup_restore_round_trip_preserves_the_moneyprinter_engine_state",
            "tests/test_moneyprinter_runtime.py::test_compose_keeps_the_runtime_opt_in",
            "tests/test_moneyprinter_runtime.py::test_moneyprinter_is_not_part_of_any_node_role",
            "tests/test_moneyprinter_runtime.py::test_configuration_lock_keeps_auto_upload_disabled",
            "tests/test_moneyprinter_runtime.py::test_pinned_reference_has_a_single_source",
        ),
    },
    {
        "id": "lifecycle",
        "title": "Optional runtime install/update/rollback, сумісність bridge/config "
                 "snapshot (container-gated)",
        "owner": "#122 §6 Lifecycle, #34/#35/#42",
        "needs": (RUNTIME,),
        "tests": (
            "tests/test_moneyprinter_runtime.py::test_release_builds_the_moneyprinter_image",
            "tests/test_moneyprinter_runtime.py::test_runtime_check_builds_the_pinned_image_without_redeclaring_pins",
            "tests/test_moneyprinter_runtime.py::test_ci_runs_the_disposable_runtime_verification",
            "tests/test_moneyprinter_runtime.py::test_entrypoint_verifies_both_bridge_versions_independently",
        ),
    },
    {
        "id": "security",
        "title": "Secrets не в DOM/logs/Job payload; artifact ACL; path traversal/непогоджений "
                 "fetch; local-only і provider allowlist; без тихого fallback",
        "owner": "#122 §6 Security, #109/#114",
        "needs": (),
        "tests": (
            "tests/test_assembly_worker_route.py::test_the_input_route_refuses_anything_but_the_owning_node_and_approved_inputs",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_reference_that_escapes_the_job_directory",
            "tests/test_assembly_worker_route.py::test_an_input_outside_the_job_directory_is_staged_not_leaked",
            "tests/test_assembly_worker_route.py::test_a_worker_without_core_delivery_refuses_instead_of_rendering",
            "tests/test_video_engines.py::test_submission_never_leaks_a_core_filesystem_path",
            "tests/test_video_engines.py::test_video_engine_is_not_substituted_when_selected_engine_lacks_an_endpoint",
            "tests/test_video_engines.py::test_remote_engine_isolation_not_configured_without_url",
            "tests/test_engine_configuration.py::test_the_read_model_exposes_no_secret_value",
            "tests/test_engine_configuration.py::test_a_secret_is_referenced_and_never_exposed",
            "tests/test_provider_switch.py::test_a_switch_that_could_carry_a_secret_in_the_endpoint_is_refused",
            "tests/test_moneyprinter_runtime.py::test_wrapper_refuses_an_unusable_voice_staging_request",
            "tests/test_moneyprinter_runtime.py::test_wrapper_refuses_an_unsafe_scene_clip_name",
            "tests/test_moneyprinter_runtime.py::test_release_gate_catches_a_re_enabled_auto_upload",
        ),
    },
    {
        "id": "configuration",
        "title": "Persist→apply→verify→rollback на фактичному executor; snapshot "
                 "застосовується і перевіряється на Worker; restart відтворює вибір",
        "owner": "#122 §5 Settings switch, #117",
        "needs": (),
        "tests": (
            "tests/test_engine_configuration.py::test_selected_effective_and_revision_describe_one_engine",
            "tests/test_engine_configuration.py::test_a_switch_is_verified_on_the_runtime_and_reported_with_its_configuration",
            "tests/test_engine_configuration.py::test_a_failed_verify_keeps_the_previous_effective_engine",
            "tests/test_engine_configuration.py::test_the_persisted_choice_is_reproduced_after_a_restart",
            "tests/test_engine_configuration.py::test_the_runtime_address_of_the_effective_engine_can_be_repointed",
            "tests/test_engine_configuration.py::test_a_refused_repointing_keeps_the_previous_runtime_address",
            "tests/test_engine_configuration.py::test_a_worker_reports_the_effective_engine_of_its_own_executor",
            "tests/test_engine_configuration.py::test_a_worker_reports_an_unready_runtime_instead_of_claiming_readiness",
            "tests/test_assembly_worker_route.py::test_a_worker_process_reports_an_unreachable_runtime_as_not_ready",
            "tests/test_assembly_worker_route.py::test_a_separate_worker_process_refuses_an_attempt_whose_engine_drifted",
            "tests/test_assembly_worker_route.py::test_worker_refuses_an_attempt_whose_engine_drifted",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_drifted_configuration_field",
            "tests/test_assembly_worker_route.py::test_the_claim_gate_covers_every_pinned_reference",
            "tests/test_assembly_worker_route.py::test_a_node_that_reports_a_drifted_configuration_is_not_handed_the_attempt",
            "tests/test_assembly_worker_route.py::test_a_node_that_does_not_report_a_pinned_reference_is_not_handed_the_attempt",
            "tests/test_assembly_worker_route.py::test_a_switch_during_an_active_attempt_does_not_change_the_dispatched_snapshot",
            "tests/test_provider_switch.py::test_applying_the_engine_through_the_real_api_is_verified_by_the_runtime",
            "tests/test_provider_switch.py::test_a_runtime_that_cannot_prove_its_inventory_keeps_the_previous_engine",
            "tests/test_provider_switch.py::test_the_applied_choice_is_reproduced_after_a_restart_of_the_core",
        ),
    },
    {
        "id": "delivery",
        "title": "Approved inputs доставляються Worker на іншому storage root з "
                 "ownership/checksum; voice staging не має гонки",
        "owner": "#122 §6 Media, P3",
        "needs": (),
        "tests": (
            "tests/test_assembly_worker_route.py::test_approved_inputs_reach_a_worker_with_a_different_storage_root",
            "tests/test_assembly_worker_route.py::test_the_delivery_base_is_the_core_address_the_worker_registered_at",
            "tests/test_assembly_worker_route.py::test_the_snapshot_records_the_dispatched_engine_and_its_revision",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_scene_that_is_not_the_approved_one",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_voice_that_is_not_the_approved_one",
            "tests/test_video_engines.py::test_each_attempt_owns_and_removes_its_staging_area",
            "tests/test_video_engines.py::test_render_refuses_a_runtime_that_cannot_stage_the_approved_voice",
            "tests/test_moneyprinter_runtime.py::test_wrapper_places_the_staged_voice_in_the_task_directory",
            "tests/test_moneyprinter_runtime.py::test_wrapper_stages_the_approved_voice_by_content_address",
            "tests/test_moneyprinter_runtime.py::test_wrapper_refuses_before_the_upstream_ever_sees_an_unstaged_voice",
            "tests/test_moneyprinter_runtime.py::test_a_staged_voice_that_no_longer_matches_its_address_is_refused_before_the_submit",
        ),
    },
    {
        "id": "browser",
        "title": "Browser: Native default, MPT configure/apply/error/rollback, "
                 "явне повернення Native, reload/restart, активний Job при switch, "
                 "admin/viewer, system-state lock",
        "owner": "#122 §6 Browser, #82/#99",
        "needs": (BROWSER,),
        # §6 Browser evidence must not substitute a mocked Settings mutation for the
        # behaviour under test (#128 §4). The repoint-runtime behaviour is proven without
        # a browser in the ``configuration`` row; the presentational ``page.route``
        # variant stays in the file but is deliberately not declared as acceptance
        # evidence. ``test_declared_browser_evidence_does_not_mock_the_tested_settings_mutation``
        # keeps this honest.
        "tests": (
            "tests/test_browser_e2e.py::test_real_backend_video_engine_shows_the_effective_executor",
            "tests/test_browser_e2e.py::test_real_backend_video_engine_refuses_a_runtime_it_cannot_prove",
            "tests/test_browser_e2e.py::test_real_backend_video_engine_applies_a_runtime_and_returns_to_native",
            "tests/test_browser_e2e.py::test_real_backend_viewer_cannot_change_the_video_engine",
            "tests/test_browser_e2e.py::test_real_backend_system_state_locks_the_video_engine",
        ),
    },
    {
        "id": "real_submit_route",
        "title": "Реальний pinned submit/status/download з гарантованим voice staging "
                 "і параметричною matrix §9 на цьому route",
        "owner": "#122 P4, §9.4/§9.5",
        "needs": (RUNTIME,),
        "tests": (
            "tests/test_moneyprinter_runtime.py::test_runtime_check_requires_a_proven_submit_route",
            "tests/test_moneyprinter_runtime.py::test_runtime_check_requires_every_readiness_gate",
            "tests/test_moneyprinter_runtime.py::test_wrapper_self_test_passes_when_every_gate_is_green",
            "tests/test_moneyprinter_runtime.py::test_the_self_test_client_serves_the_wrappers_own_routes",
            "tests/test_moneyprinter_runtime.py::test_the_submit_route_check_drives_the_upload_and_submit_routes",
        ),
    },
    {
        "id": "real_qualification",
        "title": "Реальний стенд: Job/review/Voice/assembly/дві revisions/approval з "
                 "переглядом і прослуховуванням, interruption/cancel/resource release",
        "owner": "#122 §7, P11 (ir)",
        "needs": (STAND, RUNTIME),
        "tests": (),
    },
)


def acceptance_rows() -> tuple[dict[str, Any], ...]:
    """The §6 acceptance matrix as declared data."""
    return _ACCEPTANCE_ROWS


# ---------------------------------------------------------------------------
# Identity of the tree the evidence belongs to
# ---------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    # Only trailing whitespace is dropped: ``git status --porcelain`` lines start with
    # the two status characters, and stripping the leading one would cut the first
    # character off the first path.
    return completed.stdout.rstrip()


def _repository_version() -> str:
    path = ROOT / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _dirty_paths() -> list[str]:
    output = _git("status", "--porcelain") or ""
    return [line[3:].strip().strip('"') for line in output.splitlines() if line.strip()]


def _runtime_image_digest() -> str:
    """Image digest the pinned runtime reports, when one is reachable from here."""
    from adapters.providers.runtime_manifest import INVENTORY_PATH_ENV

    path = os.environ.get(INVENTORY_PATH_ENV, "")
    if not path or not Path(path).is_file():
        return "not_available"
    try:
        from adapters.providers.runtime_manifest import load_inventory

        manifest = load_inventory(Path(path))
    except Exception:  # noqa: BLE001 - an unreadable inventory is reported, not guessed
        return "unverifiable"
    return getattr(manifest, "image_digest", "") or "not_reported"


def report_identity() -> dict[str, Any]:
    """Everything a reader needs to decide whether this evidence applies to their tree."""
    from core.engine_config import engine_config_revision

    return {
        "vertep_sha": _git("rev-parse", "HEAD") or "unknown",
        "vertep_version": _repository_version() or "unknown",
        "worktree_dirty": bool(_dirty_paths()),
        "changed_paths": _dirty_paths(),
        "pinned_upstream": PINNED_UPSTREAM_REFERENCE,
        "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        "runtime_image_digest": _runtime_image_digest(),
        "config_revision": engine_config_revision(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def sanitized_trace_id(sha: str, row_id: str) -> str:
    """Deterministic, non-identifying trace id for one criterion."""
    return hashlib.sha256(f"{sha}:{row_id}".encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Pinned M1–M4 evidence (#392 M5): one Vertep SHA, pin, digest, config revision
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json_artifact(path: Path, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Read one JSON artefact, naming it in ``problems`` instead of returning a guess."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        evidence["problems"].append(f"{path.name} is not readable JSON: {error}")
        return None
    if not isinstance(payload, dict):
        evidence["problems"].append(f"{path.name} is not a JSON object")
        return None
    return payload


def _task_count(payload: Any) -> int | None:
    """Upstream submissions read from the pinned task storage inside the runtime."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return None
    return len(payload["tasks"])


def _recovery_task_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    body = payload.get("body") or {}
    data = body.get("data") if isinstance(body, dict) else None
    return str((data or {}).get("task_id") or "") if isinstance(data, dict) else ""


def load_pinned_evidence(directory: Path | None, *,
                         identity: dict[str, Any]) -> dict[str, Any]:
    """Read the pinned M1–M4 bundle and prove it belongs to the tree under test.

    §6/#392 M5 allow a criterion to cite this bundle only when every artefact was
    produced on the same Vertep SHA with the same upstream pin, runtime image digest and
    configuration revision. A bundle that cannot attest one of them is not evidence, and
    a skipped or unexecuted case stays not-run instead of becoming a PASS.
    """
    evidence: dict[str, Any] = {
        "directory": str(directory) if directory else "",
        "present": False,
        "artifacts": {},
        "missing": [],
        "problems": [],
        "sha": "",
        "sha_attested": False,
        "image_digest": "",
        "upstream": "",
        "bridge_schema_version": "",
        "config_revision": "",
        "decoder": {},
        "submissions": {},
        "task_ids": {},
        "recovery": {"result": "", "assertions": []},
        "media_matrix": {"tests": {}, "failed": [], "skipped": [], "passed": []},
        "process_lifecycle": {},
        "links": [],
    }
    if directory is None or not str(directory).strip():
        evidence["problems"].append(
            "no pinned evidence bundle was given (--pinned-evidence)")
        return evidence
    directory = Path(directory)
    if not directory.is_dir():
        evidence["problems"].append(
            f"pinned evidence directory does not exist: {directory}")
        return evidence
    evidence["present"] = True

    missing = [name for name in PINNED_ARTEFACTS if not (directory / name).is_file()]
    evidence["missing"] = missing
    for name in PINNED_ARTEFACTS:
        path = directory / name
        if path.is_file():
            evidence["artifacts"][name] = {
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }

    snapshot = _load_json_artifact(directory / PINNED_SNAPSHOT, evidence)
    recovery = _load_json_artifact(directory / PINNED_RECOVERY, evidence)
    attested_shas: dict[str, str] = {}

    if snapshot is not None:
        snapshot_sha = str(snapshot.get("vertep_sha") or "")
        if snapshot_sha:
            attested_shas[PINNED_SNAPSHOT] = snapshot_sha
        evidence["image_digest"] = str(snapshot.get("image_digest") or "")
        evidence["config_revision"] = str(snapshot.get("config_revision") or "")
        runtime = snapshot.get("runtime") or {}
        if isinstance(runtime, dict):
            evidence["upstream"] = str(runtime.get("upstream_commit") or "")
            evidence["bridge_schema_version"] = str(
                runtime.get("bridge_schema_version") or "")
        checks = (snapshot.get("self_test") or {}).get("checks") or {}
        route = checks.get("submit_route") if isinstance(checks, dict) else None
        media = checks.get("media_pipeline") if isinstance(checks, dict) else None
        evidence["decoder"] = {
            "submit_route": {
                key: (route or {}).get(key)
                for key in ("task_id", "submit_key", "bytes", "duration_seconds",
                            "width", "height", "aspect", "fit_mode", "concat_mode",
                            "scene_order", "expected_scene_order")
            } if isinstance(route, dict) else None,
            "media_pipeline": {
                key: (media or {}).get(key)
                for key in ("bytes", "duration_seconds", "aspect", "fit_mode",
                            "scene_order", "expected_scene_order")
            } if isinstance(media, dict) else None,
        }
        evidence["task_ids"]["snapshot_submit_route"] = (
            str((route or {}).get("task_id") or "") if isinstance(route, dict) else "")

    if recovery is not None:
        recovery_sha = str(recovery.get("sha") or "")
        if recovery_sha:
            attested_shas[PINNED_RECOVERY] = recovery_sha
        assertions = recovery.get("assertions")
        evidence["recovery"] = {
            "result": str(recovery.get("result") or ""),
            "assertions": [item for item in assertions
                           if isinstance(item, dict)] if isinstance(assertions, list) else [],
        }
        recovery_digest = str(recovery.get("image_digest") or "")
        if recovery_digest:
            if evidence["image_digest"] and evidence["image_digest"] != recovery_digest:
                evidence["problems"].append(
                    f"image digest disagrees between {PINNED_SNAPSHOT} "
                    f"({evidence['image_digest']}) and {PINNED_RECOVERY} "
                    f"({recovery_digest})")
            evidence["image_digest"] = evidence["image_digest"] or recovery_digest
        for key in ("first_submit", "retried_submit", "resubmitted_after_restart",
                    "tampered_voice", "cancel"):
            count = _task_count(recovery.get(key))
            if count is not None:
                evidence["submissions"][key] = count
        missing_voice = recovery.get("missing_voice")
        if isinstance(missing_voice, dict):
            count = _task_count(missing_voice.get("retried"))
            if count is not None:
                evidence["submissions"]["missing_voice_retried"] = count
            before = missing_voice.get("tasks_before")
            if isinstance(before, list):
                evidence["submissions"]["missing_voice_before"] = len(before)
        evidence["task_ids"].update({
            "first_submit": _recovery_task_id(recovery.get("first_submit")),
            "retried_submit": _recovery_task_id(recovery.get("retried_submit")),
            "after_restart": _recovery_task_id(recovery.get("reconciled_after_restart")),
        })
        evidence["recovery"]["assertions_passed"] = sum(
            1 for item in evidence["recovery"]["assertions"] if item.get("passed"))
        evidence["recovery"]["assertions_total"] = len(
            evidence["recovery"]["assertions"])

    matrix_path = directory / PINNED_MEDIA_MATRIX
    if matrix_path.is_file():
        try:
            outcomes = _parse_junit(matrix_path)
        except (OSError, ET.ParseError) as error:
            evidence["problems"].append(
                f"{PINNED_MEDIA_MATRIX} is not readable JUnit XML: {error}")
        else:
            evidence["media_matrix"] = {
                "tests": outcomes,
                "failed": sorted(test for test, value in outcomes.items()
                                 if value == "FAILED"),
                "skipped": sorted(test for test, value in outcomes.items()
                                  if value == "SKIPPED"),
                "passed": sorted(test for test, value in outcomes.items()
                                 if value == "PASSED"),
            }

    lifecycle_path = directory / PINNED_PROCESS_LIFECYCLE
    if lifecycle_path.is_file():
        evidence["artifacts"][PINNED_PROCESS_LIFECYCLE] = {
            "sha256": _sha256_file(lifecycle_path), "bytes": lifecycle_path.stat().st_size}
        try:
            evidence["process_lifecycle"] = _parse_junit(lifecycle_path)
        except (OSError, ET.ParseError) as error:
            evidence["problems"].append(
                f"{PINNED_PROCESS_LIFECYCLE} is not readable JUnit XML: {error}")

    # One Vertep SHA: the snapshot and the recovery check have to agree with each other
    # and with the tree this report describes. Missing artefacts are recorded separately:
    # absent evidence is not-run, an artefact that contradicts the tree is a failure.
    distinct_shas = set(attested_shas.values())
    if len(distinct_shas) > 1:
        evidence["problems"].append(
            "pinned artefacts attest different Vertep SHAs: "
            + ", ".join(f"{name}={sha}" for name, sha in attested_shas.items()))
    elif distinct_shas:
        evidence["sha"] = distinct_shas.pop()
    evidence["sha_attested"] = bool(evidence["sha"])
    if evidence["sha_attested"]:
        if evidence["sha"] != identity["vertep_sha"]:
            evidence["problems"].append(
                f"pinned evidence belongs to SHA {evidence['sha']}, the tree under "
                f"test is {identity['vertep_sha']}")
    elif evidence["present"] and not missing:
        evidence["problems"].append(
            "the pinned evidence bundle does not attest its Vertep SHA")

    if snapshot is not None:
        if not evidence["config_revision"]:
            evidence["problems"].append(
                "the pinned evidence bundle does not record the configuration revision")
        elif evidence["config_revision"] != identity["config_revision"]:
            evidence["problems"].append(
                f"pinned evidence records config revision {evidence['config_revision']}, "
                f"the tree under test is {identity['config_revision']}")
        if not evidence["upstream"]:
            evidence["problems"].append(
                "the pinned evidence bundle does not record the upstream commit")
        elif evidence["upstream"] != PINNED_UPSTREAM_COMMIT:
            evidence["problems"].append(
                f"pinned evidence ran upstream {evidence['upstream']}, the pinned "
                f"reference is {PINNED_UPSTREAM_COMMIT}")
        if evidence["bridge_schema_version"] and \
                evidence["bridge_schema_version"] != BRIDGE_SCHEMA_VERSION:
            evidence["problems"].append(
                f"pinned evidence reports bridge schema "
                f"{evidence['bridge_schema_version']}, the contract is "
                f"{BRIDGE_SCHEMA_VERSION}")
    if (snapshot is not None or recovery is not None) and not evidence["image_digest"]:
        evidence["problems"].append(
            "the pinned evidence bundle records no runtime image digest")

    evidence["links"] = _pinned_links(evidence)
    return evidence


def _pinned_links(evidence: dict[str, Any]) -> list[dict[str, str]]:
    """Where each relevant §5/§6 concern is answered inside the pinned bundle."""
    present = evidence["artifacts"]
    links: list[dict[str, str]] = []

    def add(criterion: str, artifact: str, detail: str) -> None:
        if artifact in present:
            links.append({"criterion": criterion, "artifact": artifact, "detail": detail})

    add("M1", PINNED_MEDIA_MATRIX,
        "agreed §9.3 media matrix through the pinned submit/status/download route")
    add("M1", PINNED_SNAPSHOT, "self-test media pipeline and submit-route decoder report")
    add("M2", PINNED_PROCESS_LIFECYCLE,
        "CORE and stock worker.service processes: dispatch/claim/render/import, two "
        "revisions, stale/duplicate approval, Native regression")
    add("M3", PINNED_RECOVERY, "submit-key, restart, cancel and unknown-key assertions")
    add("M4", PINNED_RECOVERY, "voice staging and duplicate-key assertions across a restart")
    add("voice", PINNED_RECOVERY, "approved voice staged, tampered/missing voice refused")
    add("voice", PINNED_SNAPSHOT, "runtime reports task_local_voice capability")
    add("media", PINNED_MEDIA_MATRIX, "decoded scene order and durations")
    add("media", PINNED_SNAPSHOT, "decoded submit-route bytes, duration and geometry")
    add("config", PINNED_SNAPSHOT, "configuration revision the evidence was produced with")
    add("browser", PINNED_SNAPSHOT, "runtime readiness the Browser criterion depends on")
    add("lifecycle", PINNED_SNAPSHOT, "pinned upstream commit and runtime inventory digest")
    return links


def _pinned_status(evidence: dict[str, Any]) -> tuple[str, str]:
    """A bundle-level verdict: absent → NOT_RUN, contradicted identity → FAIL."""
    if not evidence["present"]:
        return NOT_RUN, "; ".join(evidence["problems"]) or "no pinned evidence bundle"
    if evidence["problems"]:
        return FAIL, "; ".join(evidence["problems"])
    return PASS, "the pinned bundle attests this SHA, upstream pin, digest and config"


def evaluate_pinned_criteria(evidence: dict[str, Any], *,
                             identity: dict[str, Any]) -> list[dict[str, Any]]:
    """Decide M1–M4 from the pinned bundle; a missing or skipped case is never PASS."""
    bundle_status, bundle_detail = _pinned_status(evidence)
    matrix = evidence["media_matrix"]
    matrix_tests: dict[str, str] = matrix["tests"]
    m1_tests = {test: value for test, value in matrix_tests.items()
                if test != CORRUPT_DOWNLOAD_TEST}
    m1_failed = sorted(test for test, value in m1_tests.items() if value == "FAILED")
    m1_skipped = sorted(test for test, value in m1_tests.items() if value == "SKIPPED")
    corrupt = matrix_tests.get(CORRUPT_DOWNLOAD_TEST, "")
    assertions = evidence["recovery"]["assertions"]
    assertion_checks = [str(item.get("check") or "") for item in assertions]
    failed_assertions = [str(item.get("check") or "<unnamed>") for item in assertions
                         if not item.get("passed")]
    recorded = set(assertion_checks)
    missing_assertions = [check for check in M3_REQUIRED_ASSERTIONS
                          if check not in recorded]
    busy_409 = [check for check in assertion_checks if M3_BUSY_CANCEL_MARKER in check]

    rows: list[dict[str, Any]] = []

    def row(criterion: dict[str, Any], status: str, detail: str,
            extra: dict[str, Any] | None = None) -> None:
        payload = {
            "id": criterion["id"],
            "title": criterion["title"],
            "owner": criterion["owner"],
            "status": status,
            "detail": detail,
            "artifacts": list(criterion["artefacts"]),
            "tests": list(criterion["tests"]),
            "trace_id": sanitized_trace_id(identity["vertep_sha"], criterion["id"]),
        }
        payload.update(extra or {})
        rows.append(payload)

    def missing_for(criterion: dict[str, Any]) -> list[str]:
        return [name for name in criterion["artefacts"]
                if name not in evidence["artifacts"]]

    # M1 — the pinned media matrix.
    m1 = next(item for item in PINNED_CRITERIA if item["id"] == "M1")
    m1_measure = {
        "matrix_tests": len(m1_tests),
        "matrix_passed": len([v for v in m1_tests.values() if v == "PASSED"]),
        "decoder": evidence["decoder"].get("media_pipeline"),
    }
    if bundle_status == FAIL:
        row(m1, FAIL, bundle_detail, {"measurements": m1_measure})
    elif not evidence["present"]:
        row(m1, NOT_RUN,
            "no media-matrix-pinned.xml evidence bundle was provided",
            {"measurements": m1_measure})
    elif missing_for(m1):
        row(m1, NOT_RUN,
            "pinned evidence is missing: " + ", ".join(missing_for(m1)),
            {"measurements": m1_measure})
    elif not matrix_tests:
        row(m1, NOT_RUN,
            "the pinned media matrix recorded no executable case",
            {"measurements": m1_measure})
    elif m1_failed:
        row(m1, FAIL, f"{len(m1_failed)} pinned matrix tests failed: "
                      + ", ".join(m1_failed), {"measurements": m1_measure})
    elif m1_skipped:
        row(m1, NOT_RUN, "skipped is not a pass: " + ", ".join(m1_skipped),
            {"measurements": m1_measure})
    elif not m1_tests.get(VOICE_STAGING_NEGATIVE_TEST) == "PASSED":
        row(m1, NOT_RUN,
            "the capability-negative voice-staging case was not executed on the pinned "
            "route", {"measurements": m1_measure})
    else:
        row(m1, PASS,
            f"{m1_measure['matrix_passed']} pinned matrix tests passed, including the "
            f"capability-negative voice staging case",
            {"measurements": m1_measure})

    # M2 — separate CORE/Worker processes. No pinned bundle artefact records this.
    m2 = next(item for item in PINNED_CRITERIA if item["id"] == "M2")
    lifecycle = evidence.get("process_lifecycle") or {}
    m2_measure = {"tests": {test: lifecycle.get(test, "NOT_RUN")
                            for test in PROCESS_LIFECYCLE_TESTS}}
    m2_failed = [test for test, value in m2_measure["tests"].items() if value == "FAILED"]
    m2_open = [test for test, value in m2_measure["tests"].items() if value != "PASSED"]
    if bundle_status == FAIL:
        row(m2, FAIL, bundle_detail, {"measurements": m2_measure})
    elif missing_for(m2):
        row(m2, NOT_RUN, "pinned evidence is missing: " + ", ".join(missing_for(m2)),
            {"measurements": m2_measure})
    elif m2_failed:
        row(m2, FAIL, "process lifecycle tests failed: " + ", ".join(m2_failed),
            {"measurements": m2_measure})
    elif m2_open:
        row(m2, NOT_RUN, "not executed on the pinned route (skipped is not a pass): "
            + ", ".join(m2_open), {"measurements": m2_measure})
    else:
        row(m2, PASS,
            "separate CORE and worker.service processes rendered two revisions on the "
            "pinned runtime with validated import, immutable history and stale/duplicate "
            "approval refusal; the Native route still renders in CORE",
            {"measurements": m2_measure})

    # M3 — recovery remainder.
    m3 = next(item for item in PINNED_CRITERIA if item["id"] == "M3")
    m3_measure = {
        "assertions": evidence["recovery"].get("assertions_total", 0),
        "assertions_passed": evidence["recovery"].get("assertions_passed", 0),
        "missing_assertions": missing_assertions,
        "submissions": evidence["submissions"],
        "task_ids": evidence["task_ids"],
        "decoder": evidence["decoder"].get("submit_route"),
        "process_lease_reassignment": (evidence.get("process_lifecycle") or {}).get(
            M3_PROCESS_LEASE_TEST, "NOT_RUN"),
    }
    if bundle_status == FAIL:
        row(m3, FAIL, bundle_detail, {"measurements": m3_measure})
    elif m3_measure["process_lease_reassignment"] == "FAILED":
        row(m3, FAIL, "the process lease-expiry/reassignment case failed: "
            + M3_PROCESS_LEASE_TEST, {"measurements": m3_measure})
    elif not evidence["present"]:
        row(m3, NOT_RUN, "no recovery.json evidence bundle was provided",
            {"measurements": m3_measure})
    elif missing_for(m3):
        row(m3, NOT_RUN,
            "pinned evidence is missing: " + ", ".join(missing_for(m3)),
            {"measurements": m3_measure})
    elif not evidence["recovery"]["result"]:
        row(m3, NOT_RUN, "recovery.json records no result",
            {"measurements": m3_measure})
    elif evidence["recovery"]["result"] != PASS:
        row(m3, FAIL, f"pinned recovery check reported "
                      f"{evidence['recovery']['result']}", {"measurements": m3_measure})
    elif failed_assertions:
        row(m3, FAIL, f"{len(failed_assertions)} pinned recovery assertions failed: "
                      + "; ".join(failed_assertions), {"measurements": m3_measure})
    elif missing_assertions:
        row(m3, NOT_RUN,
            "the pinned recovery bundle does not record these scenarios: "
            + "; ".join(missing_assertions), {"measurements": m3_measure})
    elif corrupt == "FAILED":
        row(m3, FAIL, "the corrupt-download case failed on the pinned runtime",
            {"measurements": m3_measure})
    elif corrupt != "PASSED":
        row(m3, NOT_RUN,
            "the corrupt-download case was not executed on the pinned runtime "
            f"(recorded: {corrupt or 'absent'})", {"measurements": m3_measure})
    elif not busy_409:
        row(m3, NOT_RUN,
            "the pinned recovery bundle records no busy DELETE 409; a 404 on an absent "
            "task, or a refusal that also accepts 404, is not that proof",
            {"measurements": m3_measure})
    else:
        row(m3, PASS,
            f"{m3_measure['assertions_passed']}/{m3_measure['assertions']} pinned "
            f"recovery assertions passed, including busy DELETE 409",
            {"measurements": m3_measure})

    # M4 — restart inside the acceptance→staging window.
    m4 = next(item for item in PINNED_CRITERIA if item["id"] == "M4")
    window_restart = [
        check for check in assertion_checks
        if ("acceptance" in check.lower() and "staging" in check.lower())
        or "before staging" in check.lower()
    ]
    m4_measure = {
        "restart_assertions": [check for check in assertion_checks if "restart" in check.lower()],
        "voice_assertions": [check for check in assertion_checks if "voice" in check.lower()],
        "submissions": evidence["submissions"],
    }
    if bundle_status == FAIL:
        row(m4, FAIL, bundle_detail, {"measurements": m4_measure})
    elif not evidence["present"]:
        row(m4, NOT_RUN, "no pinned evidence bundle was provided",
            {"measurements": m4_measure})
    elif missing_for(m4):
        row(m4, NOT_RUN,
            "pinned evidence is missing: " + ", ".join(missing_for(m4)),
            {"measurements": m4_measure})
    elif not evidence["recovery"]["result"]:
        row(m4, NOT_RUN, "recovery.json records no result",
            {"measurements": m4_measure})
    elif evidence["recovery"]["result"] != PASS:
        row(m4, FAIL, f"pinned recovery check reported "
                      f"{evidence['recovery']['result']}", {"measurements": m4_measure})
    elif failed_assertions:
        row(m4, FAIL, f"{len(failed_assertions)} pinned recovery assertions failed",
            {"measurements": m4_measure})
    elif not window_restart:
        row(m4, NOT_RUN,
            "the bundle restarts the runtime after staging, not between acceptance and "
            "staging; that window is not recorded", {"measurements": m4_measure})
    else:
        row(m4, PASS, "the acceptance→staging restart is recorded and no approved input "
                      "was lost", {"measurements": m4_measure})
    return rows


# ---------------------------------------------------------------------------
# Environment capabilities — what this machine can actually prove
# ---------------------------------------------------------------------------


def available_capabilities() -> dict[str, bool]:
    """Which of the §6 environment requirements are satisfiable here."""
    from adapters.providers.video_engines import MoneyPrinterEngine

    docker = shutil.which("docker") is not None
    try:
        # Constructing the engine reads its secret store; a host without the
        # appliance config root cannot reach a configured runtime either.
        engine = MoneyPrinterEngine()
        runtime_listening = bool(engine.configured()) and bool(
            engine.capabilities().get("ready")
        )
    except Exception:  # noqa: BLE001 - an unreachable runtime is simply absent
        runtime_listening = False
    return {
        RUNTIME: docker and runtime_listening,
        BROWSER: _browser_harness()[0],
        STAND: False,  # §7.1: a stand needs a separate authorisation of the owner.
    }


def capability_reasons() -> dict[str, str]:
    """Why a requirement is unavailable, so a NOT_RUN row names a cause."""
    from adapters.providers.video_engines import MoneyPrinterEngine

    reasons: dict[str, str] = {}
    if shutil.which("docker") is None:
        reasons[RUNTIME] = "no container runtime on this host"
    else:
        try:
            engine = MoneyPrinterEngine()
            if not engine.configured() or not engine.capabilities().get("ready"):
                reasons[RUNTIME] = "pinned runtime is not configured or not ready"
        except Exception:  # noqa: BLE001 - an unreachable runtime is simply absent
            reasons[RUNTIME] = "pinned runtime did not answer"
    _, reason = _browser_harness()
    if reason:
        reasons[BROWSER] = reason
    reasons[STAND] = "a real stand needs a separate authorisation of the owner (§7.1)"
    return reasons


def _http_json(url: str, *, user: str = "", password: str = "", timeout: float = 3.0):
    """Minimal JSON GET used only to decide whether a harness is present."""
    import base64
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url)
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                return response.status, None
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception:  # noqa: BLE001 - an unreachable host is simply absent
        return 0, None


def _browser_harness() -> tuple[bool, str]:
    """Whether the Browser E2E harness the workflow provides exists here.

    A reachable socket is not a harness: the suite needs accounts the workflow seeds and a
    server that runs the tree under test. Reporting a harness that only answers would turn
    an environment gap into a failing criterion, which §6 forbids.
    """
    base = os.getenv("VERTEP_URL", "http://127.0.0.1:8080").rstrip("/")
    if not _live_server():
        return False, f"no live CORE at {base}"
    state_dir = os.getenv("VERTEP_E2E_STATE_DIR", "").strip()
    if not state_dir or not Path(state_dir).is_dir():
        # A Browser criterion that silently skips its system-state checkpoint is not a
        # harness: reporting one would turn a missing capability into a PASS (§6).
        return False, ("VERTEP_E2E_STATE_DIR does not address the running CORE's state "
                       "store, so the system-state checkpoint would be skipped")
    user = os.getenv("VERTEP_E2E_ADMIN_USER", "e2e-admin")
    password = os.getenv("VERTEP_E2E_ADMIN_PASSWORD", "e2e-admin-password-12")
    code, _ = _http_json(f"{base}/api/session", user=user, password=password)
    if code != 200:
        return False, (f"the E2E account {user!r} cannot sign in (HTTP {code}): the "
                       f"Browser E2E harness is not running against this CORE")
    version = _repository_version()
    status, body = _http_json(f"{base}/api/status", user=user, password=password)
    if isinstance(body, dict):
        reported = str(body.get("version") or "")
        if version and reported and reported != version:
            return False, (f"the CORE at {base} runs version {reported}, "
                           f"the tree under test is {version}")
    elif status != 200:
        return False, f"{base}/api/status answered HTTP {status}"
    return True, ""


def _live_server() -> bool:
    import urllib.parse

    import socket

    parsed = urllib.parse.urlparse(os.getenv("VERTEP_URL", "http://127.0.0.1:8080"))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Running the declared evidence
# ---------------------------------------------------------------------------


def _run_pytest(node_ids: Iterable[str], junit_path: Path) -> dict[str, str]:
    """Run the declared tests and return ``node id -> outcome``."""
    node_ids = list(dict.fromkeys(node_ids))
    if not node_ids:
        return {}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--junit-xml={junit_path}", *node_ids],
        cwd=ROOT, capture_output=True, text=True,
    )
    if not junit_path.is_file():
        raise RuntimeError(
            f"pytest produced no report (exit {completed.returncode}): "
            f"{completed.stdout[-2000:]}"
        )
    return _parse_junit(junit_path)


def _canonical_node_id(classname: str, name: str) -> str:
    """Rebuild the pytest node id from a JUnit ``classname``/``name`` pair.

    JUnit records the module as a dotted path without its extension and appends the class
    chain, so ``tests.test_x.TestY`` + ``test_z`` becomes
    ``tests/test_x.py::TestY::test_z``. Acceptance criteria are declared as node ids, and
    they have to be comparable with what pytest actually ran.
    """
    parts = [part for part in str(classname or "").split(".") if part]
    classes = [part for part in parts if part[:1].isupper()]
    module = [part for part in parts if part not in classes]
    path = "/".join(module)
    if path and not path.endswith(".py"):
        path = f"{path}.py"
    chain = "::".join([*classes, name])
    return f"{path}::{chain}"


def _parse_junit(path: Path) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        node = _canonical_node_id(case.get("classname", ""), case.get("name", ""))
        if any(child.tag in {"failure", "error"} for child in case):
            outcomes[node] = "FAILED"
        elif any(child.tag == "skipped" for child in case):
            outcomes[node] = "SKIPPED"
        else:
            outcomes[node] = "PASSED"
    return outcomes


def _collected_node_ids() -> set[str]:
    """Every node id pytest can collect, used to tell "absent" from "not run"."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return {
        line.strip() for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "-"))
    }


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _collects(node_id: str, known: set[str]) -> bool:
    """A declared id is collectable directly, or as the prefix of a parameterised one.

    A criterion declares ``test_x``; pytest collects ``test_x[param]`` for a parameterised
    test. Both spellings name the same evidence, so a prefix match is the resolution and an
    unknown id stays unknown.
    """
    if node_id in known:
        return True
    return any(candidate.startswith(f"{node_id}[") for candidate in known)


def _module_of(node_id: str) -> str:
    """The file a declared node id belongs to."""
    return node_id.split("::", 1)[0]


def _module_collectable(node_id: str, known: set[str]) -> bool:
    """Whether the file of a declared id is collected here at all.

    An optional harness (Playwright, a container client) makes its whole module
    uncollectable in an environment without it. That is a missing environment, not a
    declared id that does not exist, and §6 Evidence forbids counting either as a pass —
    but the two must not be confused, or every consumer without Playwright would see the
    Browser criterion as a code failure.
    """
    module = _module_of(node_id)
    return any(candidate.startswith(f"{module}::") for candidate in known)


def _resolve_outcomes(declared: Iterable[str], outcomes: dict[str, str]) -> dict[str, str]:
    """Worst outcome of every test that answers one declared id."""
    resolved: dict[str, str] = {}
    for node_id in declared:
        if node_id in outcomes:
            resolved[node_id] = outcomes[node_id]
            continue
        matches = {outcome for candidate, outcome in outcomes.items()
                   if candidate.startswith(f"{node_id}[")}
        if not matches:
            resolved[node_id] = ""
            continue
        for candidate in ("FAILED", "SKIPPED", "PASSED"):
            if candidate in matches:
                resolved[node_id] = candidate
                break
    return resolved


def evaluate_row(row: dict[str, Any], *, outcomes: dict[str, str],
                 capabilities: dict[str, bool], identity: dict[str, Any],
                 reasons: dict[str, str] | None = None,
                 uncollectable: Iterable[str] = ()) -> dict[str, Any]:
    """Decide one criterion: PASS only with every need met and every test green."""
    resolved = _resolve_outcomes(row["tests"], outcomes)
    missing_needs = [need for need in row["needs"] if not capabilities.get(need, False)]
    unavailable = list(uncollectable)
    missing_tests = [test for test in row["tests"]
                     if not resolved.get(test) and test not in unavailable]
    failed = [test for test in row["tests"] if resolved.get(test) == "FAILED"]
    skipped = [test for test in row["tests"] if resolved.get(test) == "SKIPPED"]
    causes = [
        f"{need} ({reasons[need]})" if (reasons or {}).get(need) else need
        for need in missing_needs
    ]

    if missing_needs:
        # A criterion whose precondition this environment cannot meet is not run at all:
        # reporting its tests as failing would blame the code for a missing harness.
        status = NOT_RUN
        detail = "environment cannot provide " + ", ".join(causes)
    elif unavailable:
        # The evidence exists, but this environment cannot even collect the file that
        # holds it. That is an absent case, and an absent case is never a pass (§6).
        status = NOT_RUN
        detail = (f"{len(unavailable)} declared tests could not be collected here: "
                  + ", ".join(sorted({_module_of(test) for test in unavailable})))
    elif failed or missing_tests:
        status = FAIL
        detail = "; ".join(filter(None, [
            f"{len(failed)} declared tests failed" if failed else "",
            f"{len(missing_tests)} declared but not executed" if missing_tests else "",
        ]))
    elif skipped:
        status = NOT_RUN
        detail = f"{len(skipped)} declared tests were skipped; skipped is not a pass"
    elif not row["tests"]:
        status = NOT_RUN
        detail = "no automated evidence declared"
    else:
        status = PASS
        detail = f"{len(row['tests'])} declared tests passed"
        if identity["worktree_dirty"]:
            detail += " (on a dirty worktree: not a released SHA)"

    return {
        "id": row["id"],
        "title": row["title"],
        "owner": row["owner"],
        "status": status,
        "evidence_kind": ("authorised_real_stand" if STAND in row["needs"] else
                          "pinned_runtime" if RUNTIME in row["needs"] else "unit_or_disposable"),
        "detail": detail,
        "needs": list(row["needs"]),
        "missing_needs": missing_needs,
        "tests": list(row["tests"]),
        "failed_tests": failed,
        "skipped_tests": skipped,
        "missing_tests": missing_tests,
        "uncollectable_tests": unavailable,
        "executed": {
            test: outcome for test, outcome in resolved.items() if outcome
        },
        "trace_id": sanitized_trace_id(identity["vertep_sha"], row["id"]),
    }


def build_report(*, only: Iterable[str] = (),
                 pinned_evidence: Path | None = None) -> dict[str, Any]:
    """Collect identity, capabilities and the verdict of every §6 criterion."""
    identity = report_identity()
    capabilities = available_capabilities()
    reasons = capability_reasons()
    wanted = {value.strip() for value in only if value.strip()}
    rows = [row for row in acceptance_rows()
            if not wanted or row["id"] in wanted]

    known = _collected_node_ids()
    # Only the tests of a criterion whose environment requirements are met are executed: a
    # missing harness makes the criterion NOT_RUN, never a failure attributed to the code.
    runnable = [row for row in rows
                if all(capabilities.get(need, False) for need in row["needs"])]
    declared = [test for row in runnable for test in row["tests"]]
    unknown = sorted({test for test in declared if not _collects(test, known)})
    # A declared id whose whole module is uncollectable here (optional harness missing)
    # is an absent case rather than a code defect, and the row says so.
    uncollectable = {
        row["id"]: [test for test in row["tests"]
                    if not _collects(test, known) and not _module_collectable(test, known)]
        for row in runnable
    }

    with tempfile.TemporaryDirectory(prefix="vertep-acceptance-") as tmp:
        outcomes = _run_pytest(declared, Path(tmp) / "junit.xml")

    verdicts = []
    for row in rows:
        verdict = evaluate_row(row, outcomes=outcomes, capabilities=capabilities,
                               identity=identity, reasons=reasons,
                               uncollectable=uncollectable.get(row["id"], ()))
        verdict["undeclared_tests"] = sorted(
            test for test in row["tests"] if not _collects(test, known)
        )
        verdicts.append(verdict)

    pinned = load_pinned_evidence(pinned_evidence, identity=identity)
    pinned_rows = evaluate_pinned_criteria(pinned, identity=identity)

    return {
        "issue": "fylypovych/vertep#122",
        "stage": "P10 combined acceptance",
        "identity": identity,
        "capabilities": capabilities,
        "capability_reasons": reasons,
        "rows": verdicts,
        "pinned": pinned,
        "pinned_rows": pinned_rows,
        "summary": {
            "pass": sum(1 for item in verdicts if item["status"] == PASS),
            "fail": sum(1 for item in verdicts if item["status"] == FAIL),
            "not_run": sum(1 for item in verdicts if item["status"] == NOT_RUN),
            "declared_but_absent": unknown,
            "pinned_pass": sum(1 for item in pinned_rows if item["status"] == PASS),
            "pinned_fail": sum(1 for item in pinned_rows if item["status"] == FAIL),
            "pinned_not_run": sum(1 for item in pinned_rows if item["status"] == NOT_RUN),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    identity = report["identity"]
    lines = [
        "# Vertep #122 P10 — acceptance report (§6)",
        "",
        f"- Vertep SHA: `{identity['vertep_sha']}` (version `{identity['vertep_version']}`)",
        f"- worktree: {'dirty' if identity['worktree_dirty'] else 'clean'}",
        f"- pinned upstream: `{identity['pinned_upstream']}`",
        f"- bridge schema: `{identity['bridge_schema_version']}`",
        f"- runtime image digest: `{identity['runtime_image_digest']}`",
        f"- config revision: `{identity['config_revision']}`",
        f"- generated: {identity['generated_at']}",
        "",
        "`unit_or_disposable` перевіряє локальні contracts/orchestration. Його PASS не є "
        "доказом pinned upstream execution; для цього потрібні окремі рядки `pinned_runtime`.",
        "",
        "| Criterion | Status | Owner | Detail | Trace |",
        "|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        evidence = f" (evidence: {row['evidence_kind']})" if row.get("evidence_kind") else ""
        lines.append(
            f"| {row['id']} — {row['title']} | **{row['status']}** | {row['owner']} | "
            f"{row['detail']}{evidence} | `{row['trace_id']}` |"
        )
    pinned = report.get("pinned") or {}
    pinned_rows = report.get("pinned_rows") or []
    if pinned_rows:
        artifacts = pinned.get("artifacts") or {}
        attestation = (
            "attests this tree"
            if pinned.get("sha") and pinned.get("sha") == identity["vertep_sha"]
            and not pinned.get("problems")
            else "does not attest this tree"
        )
        lines += [
            "",
            "## Pinned evidence — M1–M4 (#389 / #392 M5)",
            "",
            f"- evidence bundle: `{pinned.get('directory') or 'not provided'}`",
            f"- evidence SHA: `{pinned.get('sha') or 'unattested'}` ({attestation})",
            f"- runtime image digest: `{pinned.get('image_digest') or 'not recorded'}`",
            f"- pinned upstream: `{pinned.get('upstream') or 'not recorded'}`",
            f"- bridge schema: `{pinned.get('bridge_schema_version') or 'not recorded'}`",
            f"- config revision: `{pinned.get('config_revision') or 'not recorded'}`",
            "",
            "| Criterion | Status | Evidence | Detail | Trace |",
            "|---|---|---|---|---|",
        ]
        for row in pinned_rows:
            lines.append(
                f"| {row['id']} — {row['title']} | **{row['status']}** | "
                f"{', '.join(f'`{name}`' for name in row['artifacts']) or '—'} | "
                f"{row['detail']} | `{row['trace_id']}` |"
            )
        if artifacts:
            lines += ["", "Evidence artefact hashes:", ""]
            for name, digest in artifacts.items():
                lines.append(f"- `{name}` — sha256 `{digest['sha256']}` ({digest['bytes']} bytes)")
        if pinned.get("links"):
            lines += ["", "Evidence links:", ""]
            for link in pinned["links"]:
                lines.append(
                    f"- {link['criterion']} → `{link['artifact']}`: {link['detail']}")
        if pinned.get("problems"):
            lines += ["", "Pinned evidence problems:", "",
                      *[f"- {problem}" for problem in pinned["problems"]]]
        if pinned.get("missing"):
            lines += ["", "Pinned evidence artefacts that were not produced:", "",
                      *[f"- `{name}`" for name in pinned["missing"]]]
    summary = report["summary"]
    lines += [
        "",
        f"PASS: {summary['pass']} · FAIL: {summary['fail']} · NOT_RUN: {summary['not_run']}",
    ]
    if pinned_rows:
        lines.append(
            f"Pinned evidence — PASS: {summary.get('pinned_pass', 0)} · "
            f"FAIL: {summary.get('pinned_fail', 0)} · "
            f"NOT_RUN: {summary.get('pinned_not_run', 0)}")
    lines += [
        "",
        "`NOT_RUN` і `skipped` не є PASS: критерій, який середовище не може довести, лишається "
        "відкритим і не зараховується у виконані пункти P10.",
    ]
    if summary["declared_but_absent"]:
        lines += [
            "",
            "Declared but not collectable tests:",
            *[f"- `{test}`" for test in summary["declared_but_absent"]],
        ]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "issue-122-p10-acceptance.json"
    markdown_path = out_dir / "issue-122-p10-acceptance.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _has_secrets(payload: str) -> bool:
    lowered = payload.lower()
    return any(marker in lowered for marker in
               ("x-api-key:", "runtime-key", "worker_secret=", "private key"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "vertep-issue-122"),
                        help="directory for the JSON and Markdown report")
    parser.add_argument("--only", default="",
                        help="comma separated criterion ids to evaluate")
    parser.add_argument("--pinned-evidence", default="",
                        help="directory of the pinned M1–M4 bundle "
                             "(runtime-snapshot.json, recovery.json, "
                             "media-matrix-pinned.xml)")
    parser.add_argument("--require-all", action="store_true",
                        help="also fail when a criterion could not be run here")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="print the Markdown report to stdout")
    args = parser.parse_args(argv)

    report = build_report(
        only=args.only.split(",") if args.only else (),
        pinned_evidence=Path(args.pinned_evidence) if args.pinned_evidence else None,
    )
    json_path, markdown_path = write_report(report, Path(args.out))

    rendered = markdown_path.read_text(encoding="utf-8")
    if _has_secrets(rendered):
        print("refusing to publish a report that contains a credential", file=sys.stderr)
        return 3

    if args.print_report:
        print(rendered, end="")
    summary = report["summary"]
    print(f"report: {json_path} and {markdown_path}")
    print(f"PASS {summary['pass']} · FAIL {summary['fail']} · NOT_RUN {summary['not_run']}")
    for row in report["rows"]:
        if row["status"] != PASS:
            print(f"  {row['status']}: {row['id']} — {row['detail']}")
    for row in report.get("pinned_rows", []):
        if row["status"] != PASS:
            print(f"  {row['status']}: {row['id']} — {row['detail']}")
    if summary["fail"] or summary.get("pinned_fail"):
        return 1
    if args.require_all and (summary["not_run"] or summary.get("pinned_not_run")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
