"""MoneyPrinterTurbo video engine on the pinned bridge contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .._http import check_response as _check
from ..base import (
    BRIDGE_SCHEMA_VERSION,
    REASON_INVENTORY_UNVERIFIED,
    REASON_SCHEMA_UNSUPPORTED,
    REASON_SNAPSHOT_MISMATCH,
    REASON_UPSTREAM_UNAUTHENTICATED,
    REASON_VOICE_STAGING_UNSUPPORTED,
    BridgeContractError,
    FIXED_SUBMIT_FIELDS,
)
from ..runtime_manifest import (
    RuntimeManifestError,
    verify_inventory,
)
from .contract import MONEY_PRINTER_CONTRACT
from .remote import RemoteVideoEngine


class MoneyPrinterEngine(RemoteVideoEngine):
    """External MoneyPrinterTurbo-style engine (MIT reference) behind VideoEngine."""

    name = "money-printer"
    env_url = "MONEY_PRINTER_URL"
    env_token = "MONEY_PRINTER_TOKEN"
    contract_profile = MONEY_PRINTER_CONTRACT

    def __init__(
        self, *args, max_read_attempts: int = 4, read_backoff: float = 0.5, **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._upload_cache: dict[str, dict] = {}
        # Bounded retry for idempotent reads only (Issue #122 §9.4).
        self._max_read_attempts = max(1, int(max_read_attempts))
        self._read_backoff = max(0.0, float(read_backoff))

    # --- runtime capability gate ---------------------------------------------

    def runtime_snapshot(self) -> dict:
        """Read the verified runtime snapshot from the wrapper."""
        response = self._transport.get(
            f"{self._url}/runtime", headers=self._headers(), timeout=30,
        )
        _check(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                "Runtime snapshot is not an object",
            )
        return payload

    def _require_task_local_voice(self) -> None:
        """Refuse the render unless the runtime can take the approved voice.

        §9.3 row 5 makes the staged voice mandatory and §9.6 forbids the runtime
        from synthesising narration of its own. The wrapper derives the answer
        from the pinned code and from its own staging route, so an incapable
        runtime is refused here — before any material is uploaded or any task is
        created.
        """
        snapshot = self.runtime_snapshot()
        capabilities = snapshot.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime does not report its staging capabilities",
            )
        if not capabilities.get("task_local_voice"):
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                capabilities.get("detail")
                or "Runtime cannot receive the approved voice audio",
            )

    def _stage_approved_voice(self, audio: Path) -> str:
        """Upload the approved narration and return its staged reference.

        The runtime stores the bytes itself and can then place them in the task
        directory of the submit that follows; Vertep never sends a filesystem
        path that the runtime could not read (§9.3 row 5).
        """
        payload = Path(audio).read_bytes()
        if not payload:
            raise BridgeContractError(
                "voice_audio_unusable",
                "Approved voice audio is empty",
            )
        response = self._transport.post(
            f"{self._url}{self.contract_profile.endpoints.get('voice', '/api/v1/voice')}",
            headers=self._headers({"x-vertep-filename": Path(audio).name}),
            content=payload,
            timeout=120,
        )
        _check(response)
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        reference = data.get("voice") if isinstance(data, dict) else None
        if not isinstance(reference, str) or not reference.strip():
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime did not return a staged voice reference",
            )
        if data.get("sha256") != hashlib.sha256(payload).hexdigest():
            raise BridgeContractError(
                REASON_VOICE_STAGING_UNSUPPORTED,
                "Runtime staged a different voice payload than the approved one",
            )
        return reference

    # --- Issue #122 P3/P4 helpers --------------------------------------------


    def _upload_materials(
        self,
        *,
        materials: list[Path],
        task_id: str,
        durations: list[float] | None = None,
    ) -> list[dict]:
        """Upload approved scene assets to the MPT runtime (Issue #122 P3).

        Each asset is uploaded once per (attempt, artifact SHA256) and the
        returned storage key carries the approved scene duration required by
        bridge contract v1 (§9.4). The cache is process-local, so a restart
        re-uploads; durable attempt mapping belongs to P6.

        Raises BridgeContractError on upload failure before submit.
        """
        profile = self.contract_profile
        if profile is None:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no pinned upstream request mapping",
            )
        if durations is not None and len(durations) != len(materials):
            raise BridgeContractError(
                "duration_count_mismatch",
                "Every material must carry its approved scene duration",
            )

        uploaded: list[dict] = []
        upload_endpoint = profile.endpoints.get("upload_material")
        if not upload_endpoint:
            raise BridgeContractError(
                "upstream_mapping_unavailable",
                f"{self.name} has no upload_material endpoint",
            )

        for index, material in enumerate(materials):
            if not material.is_file():
                raise BridgeContractError(
                    "asset_unreadable",
                    f"Scene asset is not a readable file: {material.name}",
                )

            # §9.4: every material keeps the approved duration of its scene so
            # the upstream receive window matches the approved timeline.
            scene_duration = durations[index] if durations is not None else None
            data_bytes = material.read_bytes()
            sha256 = hashlib.sha256(data_bytes).hexdigest()
            filename = material.name
            cache_key = f"{task_id}:{sha256}"

            if cache_key in self._upload_cache:
                cached = self._upload_cache[cache_key]
                uploaded.append({
                    "provider": "local",
                    "url": cached["url"],
                    "duration": scene_duration,
                    "sha256": sha256,
                    "original_name": filename,
                })
                continue

            try:
                # The wrapper is the only reachable endpoint and it builds the
                # multipart envelope the pinned API expects, so the clip travels
                # as raw bytes with its name in a header.
                response = self._transport.post(
                    f"{self._url}{upload_endpoint}",
                    headers=self._headers({"x-vertep-filename": filename}),
                    content=data_bytes,
                    timeout=300,
                )
                _check(response)
                result = response.json()
                # Pinned upstream returns {status, message, data:{file:
                # <immutable UUID storage key>}}. The storage key — not a
                # filename — is what the submit request references, so the
                # exact key of the real envelope is asserted here.
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, dict) or "file" not in data:
                    raise BridgeContractError(
                        "upstream_upload_failed",
                        f"Upload response missing data.file for {filename}",
                    )
                stored_file = data["file"]
                if not isinstance(stored_file, str) or not stored_file.strip():
                    raise BridgeContractError(
                        "upstream_upload_failed",
                        f"Upload returned an empty data.file for {filename}",
                    )
                entry = {
                    "provider": "local",
                    "url": stored_file,
                    "duration": scene_duration,
                    "sha256": sha256,
                    "original_name": filename,
                }
                self._upload_cache[cache_key] = entry
                uploaded.append(entry)
            except Exception as err:
                raise BridgeContractError(
                    "upstream_upload_failed",
                    f"Failed to upload {filename}: {err}",
                ) from err

        return uploaded

    def health_check(self) -> dict:
        """Check MPT runtime eligibility and readiness (Issue #122 P2, §9.12).

        The configured URL points at the Vertep wrapper, which is the only endpoint
        of the pinned runtime. Readiness is therefore decided by what the wrapper
        proves about the real executor, not by the fact that something answered:

        * a verified runtime snapshot — pinned commit, immutable image digest and
          bridge schema version (``engine_snapshot_mismatch`` on drift);
        * an upstream that answers 200 **with** ``x-api-key`` and refuses without it
          (``upstream_unreachable`` / ``upstream_unauthenticated``);
        * a submit schema that still declares every required ``TaskVideoRequest``
          field (``upstream_schema_unsupported``).

        Every one of these is mandatory. An unreachable, unverifiable or drifted
        runtime yields ``available`` False with an explicit reason — Issue #122
        forbids a silent fallback to the native engine for a configured engine.

        Returns:
            {
                "available": bool,
                "endpoint_reachable": bool,
                "schema_compatible": bool,
                "auth_enforced": bool,
                "bridge_version": str,
                "pinned_upstream": str,
                "runtime_manifest": dict | None,
                "snapshot_verified": bool,
                "reason": str | None,
                "error": str | None,
            }
        """
        result = {
            "available": False,
            "endpoint_reachable": False,
            "schema_compatible": False,
            "auth_enforced": False,
            "bridge_version": BRIDGE_SCHEMA_VERSION,
            "pinned_upstream": None,
            "runtime_manifest": None,
            "snapshot_verified": False,
            "reason": None,
            "error": None,
            "upstream_commit": None,
            "image_digest": None,
            "bridge_schema_version": None,
        }

        if not self._url:
            result["reason"] = "engine_not_configured"
            result["error"] = f"{self.env_url} not configured"
            return result

        profile = self.contract_profile
        if profile:
            result["pinned_upstream"] = profile.upstream_reference

        try:
            response = self._transport.get(
                f"{self._url}/health", headers=self._headers(), timeout=30
            )
            if not hasattr(response, "status_code"):
                raise TypeError(f"transport returned {type(response).__name__}, not a response")
            body = response.json()
        except Exception as err:  # noqa: BLE001 - any failure means "not ready"
            result["reason"] = "wrapper_unreachable"
            result["error"] = f"{type(err).__name__}: {err}"
            return result

        if getattr(response, "status_code", None) != 200 or not isinstance(body, dict):
            # The wrapper reports its own stable reason code when it refuses.
            checks = body.get("checks") if isinstance(body, dict) else None
            reported = (checks or {}).get("reason") if isinstance(checks, dict) else None
            result["reason"] = reported or "wrapper_unavailable"
            result["error"] = (
                (checks or {}).get("detail") if isinstance(checks, dict) else None
            ) or f"wrapper answered {getattr(response, 'status_code', 'no status')}"
            return result

        checks = body.get("checks")
        if not isinstance(checks, dict):
            result["reason"] = "wrapper_unavailable"
            result["error"] = "wrapper readiness report has no checks"
            return result

        result["endpoint_reachable"] = checks.get("upstream_auth_enforced") is True
        result["auth_enforced"] = checks.get("upstream_auth_enforced") is True
        if not result["endpoint_reachable"]:
            result["reason"] = checks.get("reason") or REASON_UPSTREAM_UNAUTHENTICATED
            result["error"] = checks.get("detail") or "upstream did not prove authenticated access"
            return result

        schema = checks.get("submit_schema")
        # The whole fixed §9.4 set, not only the fields that can start a task: a runtime
        # that stopped accepting a compose field would apply its own default to the
        # approved render while still reporting a compatible schema.
        result["schema_compatible"] = isinstance(schema, (list, dict)) and set(
            FIXED_SUBMIT_FIELDS
        ).issubset(schema)
        if not result["schema_compatible"]:
            result["reason"] = REASON_SCHEMA_UNSUPPORTED
            result["error"] = (
                "wrapper did not prove a submit schema containing "
                f"{', '.join(FIXED_SUBMIT_FIELDS)}"
            )
            return result

        snapshot = checks.get("snapshot")
        result["runtime_manifest"] = snapshot if isinstance(snapshot, dict) else None
        if not isinstance(snapshot, dict):
            result["reason"] = REASON_INVENTORY_UNVERIFIED
            result["error"] = "wrapper did not report a verified runtime inventory"
            return result

        if not self._verify_reported_manifest(snapshot, profile, result):
            return result

        result["available"] = True
        return result

    @staticmethod
    def _verify_reported_manifest(snapshot: dict, profile, result: dict) -> bool:
        """Validate a reported runtime snapshot against the pinned contract.

        Returns False and records the reason in ``result`` when the runtime drifted,
        so the caller stops with ``available`` cleared.
        """
        expected_commit = profile.upstream_reference.split("@")[-1] if profile else None
        reported_commit = snapshot.get("upstream_commit")
        result["upstream_commit"] = reported_commit
        result["image_digest"] = snapshot.get("image_digest")
        result["bridge_version"] = snapshot.get("bridge_version", BRIDGE_SCHEMA_VERSION)
        result["bridge_schema_version"] = snapshot.get("bridge_schema_version")

        if expected_commit and reported_commit != expected_commit:
            result["reason"] = REASON_INVENTORY_UNVERIFIED
            result["error"] = (
                f"pinned runtime commit drift: expected {expected_commit}, "
                f"found {reported_commit}"
            )
            result["snapshot_verified"] = False
            return False

        digest = snapshot.get("image_digest")
        if not (isinstance(digest, str) and digest.startswith("sha256:")
                and len(digest) == len("sha256:") + 64):
            result["reason"] = REASON_SNAPSHOT_MISMATCH
            result["error"] = "runtime did not report an immutable image digest"
            result["snapshot_verified"] = False
            return False

        if snapshot.get("bridge_schema_version") != BRIDGE_SCHEMA_VERSION:
            result["reason"] = REASON_SNAPSHOT_MISMATCH
            result["error"] = (
                f"expected bridge schema {BRIDGE_SCHEMA_VERSION}, "
                f"runtime reported {snapshot.get('bridge_schema_version')}"
            )
            result["snapshot_verified"] = False
            return False

        result["snapshot_verified"] = True
        return True

    def verify_local_runtime_manifest(self, path) -> dict:
        """Verify a pinned runtime inventory read from disk (Issue #122 P2).

        Used by operators and by the container self-test that has direct filesystem
        access to the image. Raises ``RuntimeManifestError`` on any drift so the
        caller refuses the engine rather than degrading.
        """
        profile = self.contract_profile
        if profile is None:
            raise RuntimeManifestError(f"{self.name} has no pinned upstream contract")
        manifest = verify_inventory(
            path,
            root=Path(path).parent,
            expected_commit=profile.upstream_reference.split("@")[-1],
            expected_bridge_version=BRIDGE_SCHEMA_VERSION,
            expected_bridge_schema_version=BRIDGE_SCHEMA_VERSION,
        )
        return {
            "upstream_reference": manifest.upstream_reference,
            "upstream_version": manifest.upstream_version,
            "bridge_version": manifest.bridge_version,
            "bridge_schema_version": manifest.bridge_schema_version,
            "dependency_count": len(manifest.dependencies),
            "inventory_digest": manifest.compute_digest(),
        }
