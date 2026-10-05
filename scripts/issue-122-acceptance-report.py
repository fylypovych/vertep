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
from adapters.providers.runtime_manifest import PINNED_UPSTREAM_REFERENCE  # noqa: E402

#: Можливі статуси рядка. ``NOT_RUN`` ніколи не рахується як PASS (§6 Evidence).
PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"

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
        ),
    },
    {
        "id": "integration",
        "title": "Штатні queue/claim/result/approval/import шляхи; один task owner",
        "owner": "#122 §6 Integration, #104",
        "needs": (),
        "tests": (
            "tests/test_assembly_worker_route.py::test_external_engine_dispatches_to_a_worker_without_rendering_in_core",
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
        "id": "lifecycle",
        "title": "Optional runtime install/update/rollback, сумісність bridge/config "
                 "snapshot, backup/restore config/secret references/mapping/artifacts",
        "owner": "#122 §6 Lifecycle, #34/#35/#42",
        "needs": (RUNTIME,),
        "tests": (
            "tests/test_moneyprinter_runtime.py::test_compose_keeps_the_runtime_opt_in",
            "tests/test_moneyprinter_runtime.py::test_release_builds_the_moneyprinter_image",
            "tests/test_moneyprinter_runtime.py::test_runtime_check_builds_the_pinned_image_without_redeclaring_pins",
            "tests/test_moneyprinter_runtime.py::test_ci_runs_the_disposable_runtime_verification",
            "tests/test_moneyprinter_runtime.py::test_entrypoint_verifies_both_bridge_versions_independently",
            "tests/test_moneyprinter_runtime.py::test_configuration_lock_keeps_auto_upload_disabled",
            "tests/test_moneyprinter_runtime.py::test_pinned_reference_has_a_single_source",
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
            "tests/test_assembly_worker_route.py::test_the_snapshot_records_the_dispatched_engine_and_its_revision",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_scene_that_is_not_the_approved_one",
            "tests/test_assembly_worker_route.py::test_worker_refuses_a_voice_that_is_not_the_approved_one",
            "tests/test_video_engines.py::test_each_attempt_owns_and_removes_its_staging_area",
            "tests/test_video_engines.py::test_render_refuses_a_runtime_that_cannot_stage_the_approved_voice",
            "tests/test_moneyprinter_runtime.py::test_wrapper_places_the_staged_voice_in_the_task_directory",
            "tests/test_moneyprinter_runtime.py::test_wrapper_stages_the_approved_voice_by_content_address",
        ),
    },
    {
        "id": "browser",
        "title": "Browser: Native default, MPT configure/apply/error/rollback, "
                 "reload/restart, активний Job при switch, admin/viewer, system-state lock",
        "owner": "#122 §6 Browser, #82/#99",
        "needs": (BROWSER,),
        "tests": (
            "tests/test_browser_e2e.py::test_real_backend_video_engine_shows_the_effective_executor",
            "tests/test_browser_e2e.py::test_real_backend_video_engine_refuses_a_runtime_it_cannot_prove",
            "tests/test_browser_e2e.py::test_real_backend_viewer_cannot_change_the_video_engine",
            "tests/test_browser_e2e.py::test_real_backend_system_state_locks_the_video_engine",
            "tests/test_browser_e2e.py::test_settings_video_engine_repoints_the_runtime_of_the_current_engine",
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
    return completed.stdout.strip()


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
# Environment capabilities — what this machine can actually prove
# ---------------------------------------------------------------------------


def available_capabilities() -> dict[str, bool]:
    """Which of the §6 environment requirements are satisfiable here."""
    from adapters.providers.video_engines import MoneyPrinterEngine

    docker = shutil.which("docker") is not None
    engine = MoneyPrinterEngine()
    try:
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
                 reasons: dict[str, str] | None = None) -> dict[str, Any]:
    """Decide one criterion: PASS only with every need met and every test green."""
    resolved = _resolve_outcomes(row["tests"], outcomes)
    missing_needs = [need for need in row["needs"] if not capabilities.get(need, False)]
    missing_tests = [test for test in row["tests"] if not resolved.get(test)]
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
        "detail": detail,
        "needs": list(row["needs"]),
        "missing_needs": missing_needs,
        "tests": list(row["tests"]),
        "failed_tests": failed,
        "skipped_tests": skipped,
        "missing_tests": missing_tests,
        "executed": {
            test: outcome for test, outcome in resolved.items() if outcome
        },
        "trace_id": sanitized_trace_id(identity["vertep_sha"], row["id"]),
    }


def build_report(*, only: Iterable[str] = ()) -> dict[str, Any]:
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

    with tempfile.TemporaryDirectory(prefix="vertep-acceptance-") as tmp:
        outcomes = _run_pytest(declared, Path(tmp) / "junit.xml")

    verdicts = []
    for row in rows:
        verdict = evaluate_row(row, outcomes=outcomes, capabilities=capabilities,
                               identity=identity, reasons=reasons)
        verdict["undeclared_tests"] = sorted(
            test for test in row["tests"] if not _collects(test, known)
        )
        verdicts.append(verdict)

    return {
        "issue": "fylypovych/vertep#122",
        "stage": "P10 combined acceptance",
        "identity": identity,
        "capabilities": capabilities,
        "capability_reasons": reasons,
        "rows": verdicts,
        "summary": {
            "pass": sum(1 for item in verdicts if item["status"] == PASS),
            "fail": sum(1 for item in verdicts if item["status"] == FAIL),
            "not_run": sum(1 for item in verdicts if item["status"] == NOT_RUN),
            "declared_but_absent": unknown,
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
        "| Criterion | Status | Owner | Detail | Trace |",
        "|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['id']} — {row['title']} | **{row['status']}** | {row['owner']} | "
            f"{row['detail']} | `{row['trace_id']}` |"
        )
    summary = report["summary"]
    lines += [
        "",
        f"PASS: {summary['pass']} · FAIL: {summary['fail']} · NOT_RUN: {summary['not_run']}",
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
    parser.add_argument("--require-all", action="store_true",
                        help="also fail when a criterion could not be run here")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="print the Markdown report to stdout")
    args = parser.parse_args(argv)

    report = build_report(only=args.only.split(",") if args.only else ())
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
    if summary["fail"]:
        return 1
    if args.require_all and summary["not_run"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())