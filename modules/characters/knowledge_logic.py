"""Character Knowledge & Memory domain logic inside Characters Module (#506 K1–K5).

Owns:
- K1: Isolated per-character knowledge source library (version, SHA256, origin, rights, quarantine).
- K2: Storage & indexing of Worker-extracted page-mapped chunks with provenance & security flags.
- K3: Risk-based approval matrix, versioned policies, immediate revocation + async index refresh,
      legal/critical holds, and incidents for already-published content.
- K4: Context API (2-level auth checks, profiles for script_agent/storyboard/voice, hybrid search
      with temporal/timeline filters, token budget, provenance, immutable Job snapshots vs live editorial).
- K5: Multi-layer memory (canon, memory/events, knowledge, beliefs/rumors, working), immutable
      versioned canon, isolated alternative timelines, explicit event transfer & canon migration
      with conflict detection and rollback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import characters_logic as cl

VERIFIED_RIGHTS = frozenset({"owned", "licensed", "public_domain"})
ALL_RIGHTS = frozenset({"unverified", "owned", "licensed", "public_domain", "quarantined", "rejected", "revoked"})
VALID_LAYERS = frozenset({"canon", "memory", "knowledge", "belief", "working"})
VALID_TRUTH_STATUSES = frozenset({"fictional_canon", "verified", "belief", "rumor", "false_belief", "unverified"})
VALID_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
VALID_REVOCATION_ACTIONS = frozenset({"block", "revoke", "correct", "delete", "restore"})

# Context profiles define allowed layers, layer priority order, and default token budgets.
CONTEXT_PROFILES: dict[str, dict[str, Any]] = {
    "script_agent": {
        "layers": ["canon", "memory", "knowledge", "belief"],
        "default_budget_tokens": 1500,
        "max_budget_tokens": 4000,
    },
    "storyboard": {
        "layers": ["canon", "memory", "knowledge", "belief"],
        "default_budget_tokens": 1200,
        "max_budget_tokens": 3000,
    },
    "voice": {
        "layers": ["canon", "belief", "memory"],
        "default_budget_tokens": 600,
        "max_budget_tokens": 1500,
    },
}

LAYER_PRIORITY = {"canon": 4, "memory": 3, "knowledge": 2, "belief": 1, "working": 0}
_WORD_RE = re.compile(r"[0-9a-zа-яєіїґ]+", re.I)

# In-memory retrieval cache invalidated synchronously on every revocation / write.
_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_val(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _tokens(text: str) -> list[str]:
    words = [w.lower() for w in _WORD_RE.findall(text or "") if len(w) >= 3]
    # Simple Cyrillic/Latin stem prefix (first 5 chars for words >= 6) improves Ukrainian/Russian recall.
    stems = list(words)
    for w in words:
        if len(w) >= 6:
            stems.append(w[:5])
    return stems


def _trigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[i:i + 3] for i in range(len(compact) - 2)}


def _estimate_tokens(text: str) -> int:
    # Conservative token estimate for Ukrainian/Latin text (~3.2 chars per token).
    return max(1, math.ceil(len(text or "") / 3.2))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, dot / (na * nb))


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class KnowledgeError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class CharacterKnowledgeStore:
    """Domain manager for Character Knowledge & Memory inside the Characters module."""

    def __init__(self, characters_root: Path | None = None):
        self.characters_root = characters_root or Path(os.getenv("CHARACTERS_ROOT", "/data/characters"))
        self.characters_root.mkdir(parents=True, exist_ok=True)

    # -- helpers & authorization -------------------------------------------------

    def _character_row(self, character_id: str) -> dict[str, Any]:
        rows = cl.query(
            "SELECT id, name, owner_brand_id, active FROM characters WHERE id = %s",
            (character_id,),
        )
        if not rows:
            raise KnowledgeError(f"Character {character_id!r} not found", 404)
        cid, name, owner_brand_id, active = rows[0]
        return {"id": cid, "name": name, "owner_brand_id": owner_brand_id, "active": bool(active)}

    def _require_owner_or_admin(self, character_id: str, actor_brand: str | None, actor_role: str) -> dict[str, Any]:
        char = self._character_row(character_id)
        if actor_role == "admin":
            return char
        if not actor_brand or char["owner_brand_id"] != actor_brand:
            raise KnowledgeError("Only character owner brand or administrator can modify knowledge", 403)
        return char

    def _require_read_access(self, character_id: str, actor_brand: str | None, actor_role: str) -> dict[str, Any]:
        char = self._character_row(character_id)
        if actor_role == "admin" or actor_role == "core":
            return char
        if actor_brand and char["owner_brand_id"] == actor_brand:
            return char
        if char["active"]:
            return char
        raise KnowledgeError("Character knowledge is not accessible to this brand", 403)

    def _audit(self, character_id: str, action: str, actor_brand: str | None, details: dict[str, Any]) -> None:
        cl.execute(
            "INSERT INTO character_audit (character_id, action, actor_brand_id, details, at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            (character_id, action, actor_brand, json.dumps(details, ensure_ascii=False)),
        )

    def _invalidate_cache(self, character_id: str) -> None:
        prefix = f"{character_id}:"
        with _CACHE_LOCK:
            for key in [k for k in _CONTEXT_CACHE if k.startswith(prefix)]:
                _CONTEXT_CACHE.pop(key, None)

    def _ensure_main_timeline(self, character_id: str, actor: str = "system") -> None:
        cl.execute(
            "INSERT INTO character_timelines (timeline_id, character_id, name, kind, description, created_by) "
            "VALUES ('main', %s, 'Основна хронологія', 'main', 'Primary character canon timeline', %s) "
            "ON CONFLICT (character_id, timeline_id) DO NOTHING",
            (character_id, actor),
        )

    def get_policy(self, character_id: str) -> dict[str, Any]:
        rows = cl.query(
            "SELECT policy_version, allow_auto_approve_low, allow_auto_approve_medium, "
            "blocked_topics, shared_with_characters, updated_by, updated_at "
            "FROM character_knowledge_policies WHERE character_id = %s "
            "ORDER BY policy_version DESC LIMIT 1",
            (character_id,),
        )
        if not rows:
            return {
                "character_id": character_id,
                "policy_version": 0,
                "allow_auto_approve_low": True,
                "allow_auto_approve_medium": False,
                "blocked_topics": [],
                "shared_with_characters": [],
                "updated_by": "default",
            }
        ver, low, med, blocked, shared, by, at = rows[0]
        return {
            "character_id": character_id,
            "policy_version": int(ver),
            "allow_auto_approve_low": bool(low),
            "allow_auto_approve_medium": bool(med),
            "blocked_topics": _json_val(blocked, []),
            "shared_with_characters": _json_val(shared, []),
            "updated_by": by,
            "updated_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
        }

    def update_policy(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        current = self.get_policy(character_id)
        next_ver = current["policy_version"] + 1
        allow_low = bool(payload.get("allow_auto_approve_low", current["allow_auto_approve_low"]))
        allow_med = bool(payload.get("allow_auto_approve_medium", current["allow_auto_approve_medium"]))
        blocked_topics = payload.get("blocked_topics", current["blocked_topics"])
        shared_chars = payload.get("shared_with_characters", current["shared_with_characters"])
        if not isinstance(blocked_topics, list) or not isinstance(shared_chars, list):
            raise KnowledgeError("blocked_topics and shared_with_characters must be lists", 422)
        cl.execute(
            "INSERT INTO character_knowledge_policies "
            "(character_id, policy_version, allow_auto_approve_low, allow_auto_approve_medium, "
            "blocked_topics, shared_with_characters, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
            (
                character_id,
                next_ver,
                allow_low,
                allow_med,
                json.dumps(blocked_topics, ensure_ascii=False),
                json.dumps(shared_chars, ensure_ascii=False),
                actor,
            ),
        )
        self._audit(character_id, "knowledge_policy_update", actor_brand, {"policy_version": next_ver, "actor": actor})
        self._invalidate_cache(character_id)
        return self.get_policy(character_id)

    # -- K1: Character Knowledge Library (Sources) -------------------------------

    def register_source(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id, actor)

        title = str(payload.get("title") or "").strip()
        if not title:
            raise KnowledgeError("title is required", 422)
        source_type = str(payload.get("source_type") or "text").strip().lower()
        language = str(payload.get("language") or "uk").strip().lower()
        origin = str(payload.get("origin") or "").strip()
        license_info = str(payload.get("license_info") or "").strip()
        requested_rights = str(payload.get("rights_status") or "unverified").strip().lower()
        if requested_rights not in ALL_RIGHTS:
            raise KnowledgeError(f"Invalid rights_status: {requested_rights}", 422)

        raw_bytes: bytes | None = None
        if payload.get("data_base64"):
            try:
                raw_bytes = base64.b64decode(payload["data_base64"])
            except Exception as error:
                raise KnowledgeError(f"Invalid data_base64: {error}", 422) from error
        elif isinstance(payload.get("content"), str):
            raw_bytes = payload["content"].encode("utf-8")

        sha256 = str(payload.get("sha256") or "").strip().lower()
        if raw_bytes is not None:
            sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if not sha256 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise KnowledgeError("content, data_base64, or valid 64-hex sha256 is required", 422)

        # Idempotent re-import check for identical fingerprint within the same character.
        existing = cl.query(
            "SELECT source_id, version, rights_status FROM character_knowledge_sources "
            "WHERE character_id = %s AND sha256 = %s ORDER BY version DESC LIMIT 1",
            (character_id, sha256),
        )
        if existing and not payload.get("force_new_version"):
            return self.get_source(character_id, existing[0][0], actor_brand=actor_brand, actor_role=actor_role)

        # Compute next version if updating an existing source_id; otherwise create new source_id.
        source_id = str(payload.get("source_id") or "").strip() or f"src-{uuid.uuid4().hex[:12]}"
        prev = cl.query(
            "SELECT MAX(version) FROM character_knowledge_sources WHERE character_id = %s AND source_id = %s",
            (character_id, source_id),
        )
        version = int(prev[0][0] or 0) + 1
        if prev and prev[0][0] is not None:
            source_id = f"{source_id}-v{version}"

        # Unverified rights MUST enter quarantine; never usable as confirmed fact.
        quarantine_reason = payload.get("quarantine_reason")
        rights_status = requested_rights
        if rights_status == "unverified":
            quarantine_reason = quarantine_reason or "Rights not verified; held in quarantine"

        file_path_rel: str | None = None
        size_bytes = int(payload.get("size_bytes") or 0)
        if raw_bytes is not None:
            size_bytes = len(raw_bytes)
            ext = {"markdown": ".md", "pdf": ".pdf", "docx": ".docx", "html": ".html"}.get(source_type, ".txt")
            target_dir = self.characters_root / character_id / "knowledge" / "sources"
            target_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{source_id}_v{version}{ext}"
            (target_dir / filename).write_bytes(raw_bytes)
            file_path_rel = f"knowledge/sources/{filename}"

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        cl.execute(
            "INSERT INTO character_knowledge_sources "
            "(source_id, character_id, title, source_type, language, version, sha256, origin, "
            "license_info, rights_status, quarantine_reason, file_path, size_bytes, page_count, "
            "metadata, created_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
            (
                source_id,
                character_id,
                title,
                source_type,
                language,
                version,
                sha256,
                origin,
                license_info,
                rights_status,
                quarantine_reason,
                file_path_rel,
                size_bytes,
                int(payload.get("page_count") or 0),
                json.dumps(metadata, ensure_ascii=False),
                actor,
            ),
        )
        self._audit(character_id, "knowledge_source_register", actor_brand, {
            "source_id": source_id, "version": version, "sha256": sha256, "rights_status": rights_status,
        })
        return self.get_source(character_id, source_id, actor_brand=actor_brand, actor_role=actor_role)

    def update_source_rights(
        self,
        character_id: str,
        source_id: str,
        rights_status: str,
        *,
        license_info: str | None = None,
        reason: str | None = None,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        status = (rights_status or "").strip().lower()
        if status not in ALL_RIGHTS:
            raise KnowledgeError(f"Invalid rights_status: {rights_status}", 422)
        src = self.get_source(character_id, source_id, actor_brand=actor_brand, actor_role=actor_role)
        q_reason = None if status in VERIFIED_RIGHTS else (reason or src.get("quarantine_reason") or "Quarantined")
        cl.execute(
            "UPDATE character_knowledge_sources "
            "SET rights_status = %s, license_info = COALESCE(%s, license_info), "
            "quarantine_reason = %s, updated_at = NOW() "
            "WHERE character_id = %s AND source_id = %s",
            (status, license_info, q_reason, character_id, source_id),
        )
        self._audit(character_id, "knowledge_source_rights", actor_brand, {
            "source_id": source_id, "rights_status": status, "reason": reason, "actor": actor,
        })
        self._invalidate_cache(character_id)
        return self.get_source(character_id, source_id, actor_brand=actor_brand, actor_role=actor_role)

    def get_source(
        self,
        character_id: str,
        source_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT source_id, character_id, title, source_type, language, version, sha256, origin, "
            "license_info, rights_status, quarantine_reason, file_path, size_bytes, page_count, "
            "metadata, created_by, created_at, updated_at "
            "FROM character_knowledge_sources WHERE character_id = %s AND source_id = %s",
            (character_id, source_id),
        )
        if not rows:
            raise KnowledgeError(f"Source {source_id!r} not found", 404)
        return self._row_to_source(rows[0])

    def list_sources(
        self,
        character_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT source_id, character_id, title, source_type, language, version, sha256, origin, "
            "license_info, rights_status, quarantine_reason, file_path, size_bytes, page_count, "
            "metadata, created_by, created_at, updated_at "
            "FROM character_knowledge_sources WHERE character_id = %s ORDER BY created_at DESC",
            (character_id,),
        )
        return [self._row_to_source(r) for r in rows]

    @staticmethod
    def _row_to_source(r: tuple) -> dict[str, Any]:
        return {
            "source_id": r[0],
            "character_id": r[1],
            "title": r[2],
            "source_type": r[3],
            "language": r[4],
            "version": int(r[5]),
            "sha256": r[6],
            "origin": r[7],
            "license_info": r[8],
            "rights_status": r[9],
            "quarantined": r[9] not in VERIFIED_RIGHTS,
            "quarantine_reason": r[10],
            "file_path": r[11],
            "size_bytes": int(r[12]),
            "page_count": int(r[13]),
            "metadata": _json_val(r[14], {}),
            "created_by": r[15],
            "created_at": r[16].isoformat() if hasattr(r[16], "isoformat") else str(r[16]),
            "updated_at": r[17].isoformat() if hasattr(r[17], "isoformat") else str(r[17]),
        }

    # -- K2: Ingest Worker-extracted chunks --------------------------------------

    def ingest_chunks(
        self,
        character_id: str,
        source_id: str,
        extraction: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        source = self.get_source(character_id, source_id, actor_brand=actor_brand, actor_role=actor_role)
        chunks = extraction.get("chunks")
        if not isinstance(chunks, list):
            raise KnowledgeError("extraction.chunks must be a list", 422)

        # Idempotent re-delivery: replace chunks for the same (source_id, source_version).
        cl.execute(
            "DELETE FROM character_knowledge_chunks WHERE character_id = %s AND source_id = %s AND source_version = %s",
            (character_id, source_id, source["version"]),
        )
        stored: list[dict[str, Any]] = []
        injection_count = 0
        for idx, chunk in enumerate(chunks):
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            chunk_id = f"chk-{source_id}-v{source['version']}-{idx}"
            page_num = chunk.get("page_number")
            section = chunk.get("section_title")
            digest = str(chunk.get("content_sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest())
            ocr_used = bool(chunk.get("ocr_used", False))
            ocr_conf = chunk.get("ocr_confidence")
            inj_flag = bool(chunk.get("injection_flagged", False))
            inj_reasons = chunk.get("injection_reasons") if isinstance(chunk.get("injection_reasons"), list) else []
            if inj_flag:
                injection_count += 1
            cl.execute(
                "INSERT INTO character_knowledge_chunks "
                "(chunk_id, source_id, character_id, source_version, page_number, section_title, "
                "chunk_index, content, content_sha256, ocr_used, ocr_confidence, injection_flagged, injection_reasons) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    chunk_id, source_id, character_id, source["version"], page_num, section,
                    idx, content, digest, ocr_used, ocr_conf, inj_flag, json.dumps(inj_reasons, ensure_ascii=False),
                ),
            )
            stored.append({
                "chunk_id": chunk_id, "page_number": page_num, "section_title": section,
                "content_sha256": digest, "ocr_used": ocr_used, "injection_flagged": inj_flag,
            })

        page_count = int(extraction.get("page_count") or source["page_count"] or 0)
        cl.execute(
            "UPDATE character_knowledge_sources SET page_count = %s, updated_at = NOW() "
            "WHERE character_id = %s AND source_id = %s",
            (page_count, character_id, source_id),
        )
        if injection_count > 0:
            cl.execute(
                "UPDATE character_knowledge_sources SET rights_status = 'quarantined', "
                "quarantine_reason = 'Prompt injection pattern detected in extracted text', updated_at = NOW() "
                "WHERE character_id = %s AND source_id = %s",
                (character_id, source_id),
            )
        self._audit(character_id, "knowledge_chunks_ingest", actor_brand, {
            "source_id": source_id, "chunks": len(stored), "injection_flagged": injection_count, "actor": actor,
        })
        self._invalidate_cache(character_id)
        return {"source_id": source_id, "source_version": source["version"], "ingested": len(stored),
                "injection_flagged": injection_count, "chunks": stored}

    def list_chunks(
        self,
        character_id: str,
        source_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT chunk_id, source_id, source_version, page_number, section_title, chunk_index, "
            "content, content_sha256, ocr_used, ocr_confidence, injection_flagged, injection_reasons "
            "FROM character_knowledge_chunks WHERE character_id = %s AND source_id = %s ORDER BY chunk_index",
            (character_id, source_id),
        )
        return [
            {
                "chunk_id": r[0], "source_id": r[1], "source_version": int(r[2]),
                "page_number": r[3], "section_title": r[4], "chunk_index": int(r[5]),
                "content": r[6], "content_sha256": r[7], "ocr_used": bool(r[8]),
                "ocr_confidence": r[9], "injection_flagged": bool(r[10]),
                "injection_reasons": _json_val(r[11], []),
            }
            for r in rows
        ]

    # -- K3: Structured Knowledge Items & Risk-based Approval --------------------

    def _assess_risk(
        self,
        *,
        layer: str,
        source: dict[str, Any] | None,
        safety_flags: list[str],
        requested_risk: str | None,
        blocked_topics: list[str],
        content: str,
    ) -> tuple[str, list[str]]:
        flags = list(safety_flags)
        lowered = content.lower()
        for topic in blocked_topics:
            if str(topic).strip().lower() and str(topic).strip().lower() in lowered:
                flags.append(f"blocked_topic:{topic}")

        if flags:
            return "critical", flags
        if source is not None and source["rights_status"] not in VERIFIED_RIGHTS:
            return "critical", flags + ["unverified_source_rights"]
        if layer == "canon":
            return "high" if requested_risk != "critical" else "critical", flags
        if requested_risk in VALID_RISK_LEVELS:
            return requested_risk, flags
        if source is not None and source["rights_status"] in VERIFIED_RIGHTS and layer in {"knowledge", "belief"}:
            return "low", flags
        return "medium", flags

    def create_item(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id, actor)

        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not title or not content:
            raise KnowledgeError("title and content are required", 422)

        layer = str(payload.get("layer") or "knowledge").strip().lower()
        if layer not in VALID_LAYERS:
            raise KnowledgeError(f"Invalid layer: {layer}", 422)
        item_type = str(payload.get("item_type") or ("event" if layer == "memory" else "fact")).strip().lower()
        truth_status = str(payload.get("truth_status") or (
            "fictional_canon" if layer == "canon" else ("belief" if layer == "belief" else "verified")
        )).strip().lower()
        if truth_status not in VALID_TRUTH_STATUSES:
            raise KnowledgeError(f"Invalid truth_status: {truth_status}", 422)

        timeline_id = str(payload.get("timeline_id") or "main").strip()
        if timeline_id != "main":
            t_rows = cl.query(
                "SELECT 1 FROM character_timelines WHERE character_id = %s AND timeline_id = %s",
                (character_id, timeline_id),
            )
            if not t_rows:
                raise KnowledgeError(f"Timeline {timeline_id!r} not found", 404)

        source_id = payload.get("source_id")
        source: dict[str, Any] | None = None
        if source_id:
            source = self.get_source(character_id, str(source_id), actor_brand=actor_brand, actor_role=actor_role)
            # Check if any chunk of this source had blocking injection flags.
            inj = cl.query(
                "SELECT 1 FROM character_knowledge_chunks "
                "WHERE character_id = %s AND source_id = %s AND injection_flagged = TRUE LIMIT 1",
                (character_id, str(source_id)),
            )
            if inj:
                payload_flags = list(payload.get("safety_flags") or []) + ["prompt_injection_source"]
                payload = {**payload, "safety_flags": payload_flags}

        policy = self.get_policy(character_id)
        safety_flags = [str(f) for f in (payload.get("safety_flags") or [])]
        risk_level, final_flags = self._assess_risk(
            layer=layer,
            source=source,
            safety_flags=safety_flags,
            requested_risk=payload.get("risk_level"),
            blocked_topics=policy["blocked_topics"],
            content=f"{title}\n{content}",
        )

        # Contract 3: Risk-based Approval rules
        approved_by = None
        approved_at_sql = "NULL"
        if source is not None and source["rights_status"] not in VERIFIED_RIGHTS:
            status = "quarantined"
            if truth_status == "verified":
                truth_status = "unverified"
        elif any(f.startswith("prompt_injection") or f.startswith("blocked_topic") for f in final_flags):
            status = "quarantined"
        elif layer == "canon":
            # Contract 2: Canon changes are always saved as drafts first!
            status = "draft"
        elif risk_level == "low" and policy["allow_auto_approve_low"]:
            status = "approved"
            approved_by = "auto:low_risk_policy"
            approved_at_sql = "NOW()"
        elif risk_level == "medium" and policy["allow_auto_approve_medium"]:
            status = "approved"
            approved_by = "auto:medium_risk_policy"
            approved_at_sql = "NOW()"
        else:
            status = "pending_approval" if payload.get("submit_for_approval", True) else "draft"

        provenance = dict(payload.get("provenance") or {})
        if source is not None:
            provenance.setdefault("source_id", source["source_id"])
            provenance.setdefault("source_title", source["title"])
            provenance.setdefault("source_version", source["version"])
            provenance.setdefault("source_sha256", source["sha256"])
            provenance.setdefault("rights_status", source["rights_status"])
        if payload.get("page_number") is not None:
            provenance["page_number"] = int(payload["page_number"])
        if payload.get("scene_id"):
            provenance["scene_id"] = str(payload["scene_id"])
        if payload.get("job_id"):
            provenance["job_id"] = str(payload["job_id"])
        if isinstance(payload.get("embedding"), list):
            provenance["embedding"] = [float(x) for x in payload["embedding"]]

        item_id = f"ki-{uuid.uuid4().hex[:12]}"
        cl.execute(
            f"INSERT INTO character_knowledge_items "
            f"(item_id, character_id, source_id, source_version, page_number, layer, item_type, "
            f"truth_status, title, content, provenance, confidence, risk_level, status, version, "
            f"timeline_id, timeline_order, scene_id, job_id, safety_flags, approved_by, approved_at, "
            f"created_by, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, {approved_at_sql}, %s, NOW(), NOW())",
            (
                item_id,
                character_id,
                source["source_id"] if source else None,
                source["version"] if source else None,
                int(payload["page_number"]) if payload.get("page_number") is not None else None,
                layer,
                item_type,
                truth_status,
                title,
                content,
                json.dumps(provenance, ensure_ascii=False),
                float(payload.get("confidence", 0.85)),
                risk_level,
                status,
                timeline_id,
                int(payload.get("timeline_order") or 0),
                payload.get("scene_id"),
                payload.get("job_id"),
                json.dumps(final_flags, ensure_ascii=False),
                approved_by,
                actor,
            ),
        )
        self._audit(character_id, "knowledge_item_create", actor_brand, {
            "item_id": item_id, "layer": layer, "risk_level": risk_level, "status": status,
        })
        self._invalidate_cache(character_id)
        return self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)

    def approve_item(
        self,
        character_id: str,
        item_id: str,
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
        approver_level: str = "editor",
    ) -> dict[str, Any]:
        char = self._require_owner_or_admin(character_id, actor_brand, actor_role)
        item = self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)
        if item["status"] in {"revoked", "deleted"}:
            raise KnowledgeError("Revoked or deleted item cannot be approved directly; restore first", 409)
        if item["source_id"]:
            source = self.get_source(character_id, item["source_id"], actor_brand=actor_brand, actor_role=actor_role)
            if source["rights_status"] not in VERIFIED_RIGHTS:
                raise KnowledgeError("Cannot approve knowledge item whose source rights are unverified/quarantined", 409)
        if any(str(f).startswith("prompt_injection") for f in item["safety_flags"]):
            raise KnowledgeError("Item blocked by prompt-injection check cannot be approved", 409)

        # Contract 3 role authority matrix:
        # - high: authorized editor (moderator of owner brand or admin)
        # - critical: owner / admin (or explicitly delegated approver)
        if item["risk_level"] == "critical":
            is_owner_approver = actor_role == "admin" or (
                actor_brand == char["owner_brand_id"] and approver_level in {"owner", "delegated_owner"}
            )
            if not is_owner_approver:
                raise KnowledgeError("Critical risk item requires character owner or administrator approval", 403)
        elif item["risk_level"] == "high":
            if actor_role != "admin" and approver_level not in {"editor", "owner", "delegated_owner"}:
                raise KnowledgeError("High risk item requires authorized editor approval", 403)

        cl.execute(
            "UPDATE character_knowledge_items "
            "SET status = 'approved', approved_by = %s, approved_at = NOW(), updated_at = NOW() "
            "WHERE character_id = %s AND item_id = %s",
            (actor, character_id, item_id),
        )
        self._audit(character_id, "knowledge_item_approve", actor_brand, {
            "item_id": item_id, "risk_level": item["risk_level"], "actor": actor,
        })
        self._invalidate_cache(character_id)
        return self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)

    def reject_item(
        self,
        character_id: str,
        item_id: str,
        *,
        reason: str,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)
        cl.execute(
            "UPDATE character_knowledge_items "
            "SET status = 'blocked', revocation_reason = %s, updated_at = NOW() "
            "WHERE character_id = %s AND item_id = %s",
            (reason or "Rejected", character_id, item_id),
        )
        self._audit(character_id, "knowledge_item_reject", actor_brand, {
            "item_id": item_id, "reason": reason, "actor": actor,
        })
        self._invalidate_cache(character_id)
        return self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)

    def get_item(
        self,
        character_id: str,
        item_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT item_id, character_id, source_id, source_version, page_number, layer, item_type, "
            "truth_status, title, content, provenance, confidence, risk_level, status, version, "
            "timeline_id, timeline_order, scene_id, job_id, safety_flags, approved_by, approved_at, "
            "revoked_by, revoked_at, revocation_reason, legal_hold, created_by, created_at, updated_at "
            "FROM character_knowledge_items WHERE character_id = %s AND item_id = %s",
            (character_id, item_id),
        )
        if not rows:
            raise KnowledgeError(f"Knowledge item {item_id!r} not found", 404)
        return self._row_to_item(rows[0])

    def list_items(
        self,
        character_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
        layer: str | None = None,
        status: str | None = None,
        timeline_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        sql = (
            "SELECT item_id, character_id, source_id, source_version, page_number, layer, item_type, "
            "truth_status, title, content, provenance, confidence, risk_level, status, version, "
            "timeline_id, timeline_order, scene_id, job_id, safety_flags, approved_by, approved_at, "
            "revoked_by, revoked_at, revocation_reason, legal_hold, created_by, created_at, updated_at "
            "FROM character_knowledge_items WHERE character_id = %s"
        )
        params: list[Any] = [character_id]
        if layer:
            sql += " AND layer = %s"
            params.append(layer)
        if status:
            sql += " AND status = %s"
            params.append(status)
        if timeline_id:
            sql += " AND timeline_id = %s"
            params.append(timeline_id)
        sql += " ORDER BY timeline_order ASC, created_at DESC"
        return [self._row_to_item(r) for r in cl.query(sql, tuple(params))]

    @staticmethod
    def _row_to_item(r: tuple) -> dict[str, Any]:
        return {
            "item_id": r[0], "character_id": r[1], "source_id": r[2],
            "source_version": int(r[3]) if r[3] is not None else None,
            "page_number": int(r[4]) if r[4] is not None else None,
            "layer": r[5], "item_type": r[6], "truth_status": r[7],
            "title": r[8], "content": r[9], "provenance": _json_val(r[10], {}),
            "confidence": float(r[11]), "risk_level": r[12], "status": r[13],
            "version": int(r[14]), "timeline_id": r[15], "timeline_order": int(r[16]),
            "scene_id": r[17], "job_id": r[18], "safety_flags": _json_val(r[19], []),
            "approved_by": r[20],
            "approved_at": r[21].isoformat() if hasattr(r[21], "isoformat") else r[21],
            "revoked_by": r[22],
            "revoked_at": r[23].isoformat() if hasattr(r[23], "isoformat") else r[23],
            "revocation_reason": r[24], "legal_hold": bool(r[25]), "created_by": r[26],
            "created_at": r[27].isoformat() if hasattr(r[27], "isoformat") else str(r[27]),
            "updated_at": r[28].isoformat() if hasattr(r[28], "isoformat") else str(r[28]),
        }

    # -- K3 / Contract 4: Knowledge Revocation & Incidents -----------------------

    def apply_revocation(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        target_type = str(payload.get("target_type") or "").strip().lower()
        target_id = str(payload.get("target_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        reason = str(payload.get("reason") or "").strip()
        if target_type not in {"source", "item"} or not target_id:
            raise KnowledgeError("target_type ('source'|'item') and target_id are required", 422)
        if action not in VALID_REVOCATION_ACTIONS:
            raise KnowledgeError(f"Invalid revocation action: {action}", 422)
        if not reason:
            raise KnowledgeError("reason is required for revocation/restore", 422)

        legal_block = bool(payload.get("legal_block", False))
        critical_block = bool(payload.get("critical_block", False))

        # Final revocation / delete / restore requires policy authority (admin or owner brand).
        if action in {"delete", "restore"} and actor_role != "admin" and not actor_brand:
            raise KnowledgeError("Policy authority required for delete/restore", 403)

        if target_type == "source":
            source = self.get_source(character_id, target_id, actor_brand=actor_brand, actor_role=actor_role)
            if action == "block":
                cl.execute(
                    "UPDATE character_knowledge_sources SET rights_status = 'quarantined', "
                    "quarantine_reason = %s, updated_at = NOW() WHERE character_id = %s AND source_id = %s",
                    (reason, character_id, target_id),
                )
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'blocked', legal_hold = %s, "
                    "revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND source_id = %s AND status = 'approved'",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "revoke":
                cl.execute(
                    "UPDATE character_knowledge_sources SET rights_status = 'revoked', "
                    "quarantine_reason = %s, updated_at = NOW() WHERE character_id = %s AND source_id = %s",
                    (reason, character_id, target_id),
                )
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'revoked', legal_hold = %s, "
                    "revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND source_id = %s",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "delete":
                # Keep minimal audit & provenance; delete raw file and content.
                if source.get("file_path"):
                    raw_path = self.characters_root / character_id / source["file_path"]
                    if raw_path.is_file():
                        raw_path.unlink()
                cl.execute(
                    "DELETE FROM character_knowledge_chunks WHERE character_id = %s AND source_id = %s",
                    (character_id, target_id),
                )
                cl.execute(
                    "UPDATE character_knowledge_sources SET rights_status = 'revoked', file_path = NULL, "
                    "quarantine_reason = %s, updated_at = NOW() WHERE character_id = %s AND source_id = %s",
                    (f"deleted: {reason}", character_id, target_id),
                )
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'deleted', content = '[REDACTED]', "
                    "legal_hold = %s, revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND source_id = %s",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "restore":
                cl.execute(
                    "UPDATE character_knowledge_sources SET rights_status = 'licensed', "
                    "quarantine_reason = NULL, updated_at = NOW() WHERE character_id = %s AND source_id = %s",
                    (character_id, target_id),
                )
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'approved', legal_hold = FALSE, "
                    "revoked_by = NULL, revoked_at = NULL, revocation_reason = NULL, updated_at = NOW() "
                    "WHERE character_id = %s AND source_id = %s AND status IN ('blocked', 'revoked')",
                    (character_id, target_id),
                )
        else:
            item = self.get_item(character_id, target_id, actor_brand=actor_brand, actor_role=actor_role)
            if action == "block":
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'blocked', legal_hold = %s, "
                    "revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND item_id = %s",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "revoke":
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'revoked', legal_hold = %s, "
                    "revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND item_id = %s",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "correct":
                new_content = str(payload.get("corrected_content") or "").strip()
                if not new_content:
                    raise KnowledgeError("corrected_content is required for action='correct'", 422)
                cl.execute(
                    "UPDATE character_knowledge_items SET content = %s, version = version + 1, "
                    "status = 'pending_approval', revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND item_id = %s",
                    (new_content, reason, character_id, target_id),
                )
            elif action == "delete":
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'deleted', content = '[REDACTED]', "
                    "legal_hold = %s, revoked_by = %s, revoked_at = NOW(), revocation_reason = %s, updated_at = NOW() "
                    "WHERE character_id = %s AND item_id = %s",
                    (legal_block or critical_block, actor, reason, character_id, target_id),
                )
            elif action == "restore":
                if item["status"] == "deleted":
                    raise KnowledgeError("Deleted item content was removed and cannot be restored", 409)
                cl.execute(
                    "UPDATE character_knowledge_items SET status = 'approved', legal_hold = FALSE, "
                    "revoked_by = NULL, revoked_at = NULL, revocation_reason = NULL, updated_at = NOW() "
                    "WHERE character_id = %s AND item_id = %s",
                    (character_id, target_id),
                )

        # Immediate cache invalidation + record revocation and async index rebuild marker.
        self._invalidate_cache(character_id)
        revocation_id = f"rev-{uuid.uuid4().hex[:12]}"
        cl.execute(
            "INSERT INTO character_knowledge_revocations "
            "(revocation_id, character_id, target_type, target_id, action, reason, legal_block, "
            "critical_block, actor, actor_role, cache_invalidated_at, index_rebuilt_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())",
            (
                revocation_id, character_id, target_type, target_id, action, reason,
                legal_block, critical_block, actor, actor_role,
            ),
        )
        self._audit(character_id, f"knowledge_{action}", actor_brand, {
            "revocation_id": revocation_id, "target_type": target_type, "target_id": target_id,
            "legal_block": legal_block, "critical_block": critical_block, "reason": reason,
        })

        # Clarification #4: if published_job_ids are supplied for legal/critical revocation,
        # never mutate published content; create an incident for authorized review instead.
        incidents: list[dict[str, Any]] = []
        if (legal_block or critical_block) and action in {"block", "revoke", "delete"}:
            for pub_job_id in payload.get("published_job_ids") or []:
                incidents.append(self.create_incident(
                    character_id,
                    job_id=str(pub_job_id),
                    revocation_id=revocation_id,
                    severity="critical",
                    reason=f"Published content references revoked {target_type} {target_id}: {reason}",
                    actor=actor,
                ))

        return {
            "revocation_id": revocation_id,
            "character_id": character_id,
            "target_type": target_type,
            "target_id": target_id,
            "action": action,
            "legal_block": legal_block,
            "critical_block": critical_block,
            "cache_invalidated": True,
            "index_rebuilt": True,
            "incidents": incidents,
        }

    def create_incident(
        self,
        character_id: str,
        *,
        job_id: str,
        revocation_id: str | None = None,
        severity: str = "critical",
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        cl.execute(
            "INSERT INTO character_knowledge_incidents "
            "(incident_id, character_id, job_id, revocation_id, severity, reason, status, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, NOW())",
            (incident_id, character_id, job_id, revocation_id, severity, reason, actor),
        )
        self._audit(character_id, "knowledge_incident_open", None, {
            "incident_id": incident_id, "job_id": job_id, "revocation_id": revocation_id, "severity": severity,
        })
        return {
            "incident_id": incident_id, "character_id": character_id, "job_id": job_id,
            "revocation_id": revocation_id, "severity": severity, "reason": reason, "status": "open",
        }

    def list_incidents(self, character_id: str, *, actor_brand: str | None, actor_role: str) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT incident_id, character_id, job_id, revocation_id, severity, reason, status, created_by, created_at "
            "FROM character_knowledge_incidents WHERE character_id = %s ORDER BY created_at DESC",
            (character_id,),
        )
        return [
            {
                "incident_id": r[0], "character_id": r[1], "job_id": r[2], "revocation_id": r[3],
                "severity": r[4], "reason": r[5], "status": r[6], "created_by": r[7],
                "created_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]),
            }
            for r in rows
        ]

    # -- K5: Timelines, Versioned Canon, Event Transfer & Migration --------------

    def list_timelines(self, character_id: str, *, actor_brand: str | None, actor_role: str) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id)
        rows = cl.query(
            "SELECT timeline_id, character_id, name, kind, parent_timeline_id, branch_point_order, description, created_by, created_at "
            "FROM character_timelines WHERE character_id = %s ORDER BY kind DESC, created_at ASC",
            (character_id,),
        )
        return [
            {
                "timeline_id": r[0], "character_id": r[1], "name": r[2], "kind": r[3],
                "parent_timeline_id": r[4], "branch_point_order": r[5], "description": r[6],
                "created_by": r[7], "created_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]),
            }
            for r in rows
        ]

    def create_timeline(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id, actor)
        timeline_id = str(payload.get("timeline_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not timeline_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", timeline_id):
            raise KnowledgeError("Invalid timeline_id", 422)
        if timeline_id == "main":
            raise KnowledgeError("Main timeline already exists; only one primary timeline is allowed", 409)
        if not name:
            raise KnowledgeError("name is required", 422)
        parent_id = str(payload.get("parent_timeline_id") or "main").strip()
        branch_point = int(payload.get("branch_point_order") or 0)
        description = str(payload.get("description") or "")
        existing = cl.query(
            "SELECT 1 FROM character_timelines WHERE character_id = %s AND timeline_id = %s",
            (character_id, timeline_id),
        )
        if existing:
            raise KnowledgeError(f"Timeline {timeline_id!r} already exists", 409)
        cl.execute(
            "INSERT INTO character_timelines "
            "(timeline_id, character_id, name, kind, parent_timeline_id, branch_point_order, description, created_by) "
            "VALUES (%s, %s, %s, 'alternative', %s, %s, %s, %s)",
            (timeline_id, character_id, name, parent_id, branch_point, description, actor),
        )
        self._audit(character_id, "timeline_create", actor_brand, {
            "timeline_id": timeline_id, "parent_timeline_id": parent_id, "branch_point_order": branch_point,
        })
        return next(t for t in self.list_timelines(character_id, actor_brand=actor_brand, actor_role=actor_role)
                    if t["timeline_id"] == timeline_id)

    def publish_canon_version(
        self,
        character_id: str,
        *,
        timeline_id: str = "main",
        note: str = "",
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id, actor)
        # Approve any ready canon drafts on this timeline only if explicitly approved or already approved.
        approved_items = self.list_items(
            character_id, actor_brand=actor_brand, actor_role=actor_role,
            layer="canon", status="approved", timeline_id=timeline_id,
        )
        if not approved_items:
            raise KnowledgeError("No approved canon items available to publish a canon version", 422)
        max_row = cl.query(
            "SELECT MAX(canon_version) FROM character_canon_versions WHERE character_id = %s AND timeline_id = %s",
            (character_id, timeline_id),
        )
        next_ver = int(max_row[0][0] or 0) + 1
        snapshot = {
            "character_id": character_id,
            "timeline_id": timeline_id,
            "canon_version": next_ver,
            "items": [
                {
                    "item_id": it["item_id"],
                    "version": it["version"],
                    "title": it["title"],
                    "content": it["content"],
                    "truth_status": it["truth_status"],
                    "timeline_order": it["timeline_order"],
                    "provenance": it["provenance"],
                }
                for it in approved_items
            ],
        }
        digest = _canonical_sha256(snapshot)
        cl.execute(
            "INSERT INTO character_canon_versions "
            "(character_id, timeline_id, canon_version, snapshot, snapshot_sha256, approved_by, note, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
            (character_id, timeline_id, next_ver, json.dumps(snapshot, ensure_ascii=False), digest, actor, note),
        )
        self._audit(character_id, "canon_publish", actor_brand, {
            "timeline_id": timeline_id, "canon_version": next_ver, "snapshot_sha256": digest, "items": len(approved_items),
        })
        self._invalidate_cache(character_id)
        return {
            "character_id": character_id,
            "timeline_id": timeline_id,
            "canon_version": next_ver,
            "snapshot_sha256": digest,
            "approved_by": actor,
            "note": note,
            "items_count": len(approved_items),
            "snapshot": snapshot,
        }

    def list_canon_versions(
        self,
        character_id: str,
        *,
        timeline_id: str = "main",
        actor_brand: str | None,
        actor_role: str,
    ) -> list[dict[str, Any]]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT canon_version, snapshot, snapshot_sha256, approved_by, note, created_at "
            "FROM character_canon_versions WHERE character_id = %s AND timeline_id = %s "
            "ORDER BY canon_version DESC",
            (character_id, timeline_id),
        )
        return [
            {
                "character_id": character_id, "timeline_id": timeline_id,
                "canon_version": int(r[0]), "snapshot": _json_val(r[1], {}),
                "snapshot_sha256": r[2], "approved_by": r[3], "note": r[4],
                "created_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
            }
            for r in rows
        ]

    def transfer_event(
        self,
        character_id: str,
        *,
        item_id: str,
        target_timeline_id: str,
        allow_conflict: bool = False,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        """Contract 2: explicit approved transfer of an event across timelines with contradiction check."""
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        source_item = self.get_item(character_id, item_id, actor_brand=actor_brand, actor_role=actor_role)
        if source_item["status"] != "approved":
            raise KnowledgeError("Only approved items may be transferred between timelines", 409)
        if source_item["timeline_id"] == target_timeline_id:
            raise KnowledgeError("Target timeline matches source timeline", 422)

        existing_target = self.list_items(
            character_id, actor_brand=actor_brand, actor_role=actor_role,
            status="approved", timeline_id=target_timeline_id,
        )
        conflicts: list[dict[str, Any]] = []
        src_title_norm = source_item["title"].strip().lower()
        for other in existing_target:
            same_title = other["title"].strip().lower() == src_title_norm
            same_order_conflict = (
                other["layer"] == source_item["layer"]
                and other["timeline_order"] == source_item["timeline_order"]
                and other["content"].strip().lower() != source_item["content"].strip().lower()
            )
            if same_title and other["content"].strip().lower() != source_item["content"].strip().lower():
                conflicts.append({"conflict_with": other["item_id"], "reason": "contradicting_title_content"})
            elif same_order_conflict and source_item["timeline_order"] > 0:
                conflicts.append({"conflict_with": other["item_id"], "reason": "same_timeline_order_conflict"})

        if conflicts and not allow_conflict:
            raise KnowledgeError(
                json.dumps({"error": "timeline_conflict", "conflicts": conflicts}, ensure_ascii=False),
                409,
            )

        provenance = {
            **source_item["provenance"],
            "transferred_from_timeline": source_item["timeline_id"],
            "transferred_from_item_id": source_item["item_id"],
            "conflicts_acknowledged": conflicts,
        }
        new_item_id = f"ki-{uuid.uuid4().hex[:12]}"
        cl.execute(
            "INSERT INTO character_knowledge_items "
            "(item_id, character_id, source_id, source_version, page_number, layer, item_type, "
            "truth_status, title, content, provenance, confidence, risk_level, status, version, "
            "timeline_id, timeline_order, scene_id, job_id, safety_flags, approved_by, approved_at, "
            "created_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', 1, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), NOW())",
            (
                new_item_id, character_id, source_item["source_id"], source_item["source_version"],
                source_item["page_number"], source_item["layer"], source_item["item_type"],
                source_item["truth_status"], source_item["title"], source_item["content"],
                json.dumps(provenance, ensure_ascii=False), source_item["confidence"],
                source_item["risk_level"], target_timeline_id, source_item["timeline_order"],
                source_item["scene_id"], source_item["job_id"],
                json.dumps(source_item["safety_flags"], ensure_ascii=False), actor, actor,
            ),
        )
        self._audit(character_id, "timeline_transfer", actor_brand, {
            "from_item": item_id, "to_item": new_item_id, "target_timeline_id": target_timeline_id,
            "conflicts": conflicts,
        })
        self._invalidate_cache(character_id)
        return {
            "transferred_item": self.get_item(character_id, new_item_id, actor_brand=actor_brand, actor_role=actor_role),
            "conflicts": conflicts,
        }

    def check_and_migrate_canon(
        self,
        character_id: str,
        *,
        timeline_id: str = "main",
        from_version: int,
        to_version: int,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        """Contract 2: explicit canon version migration / rollback compatibility report."""
        self._require_owner_or_admin(character_id, actor_brand, actor_role)
        versions = {v["canon_version"]: v for v in self.list_canon_versions(
            character_id, timeline_id=timeline_id, actor_brand=actor_brand, actor_role=actor_role
        )}
        if from_version not in versions or to_version not in versions:
            raise KnowledgeError("Requested canon_version not found", 404)
        src_items = {it["title"].lower(): it for it in versions[from_version]["snapshot"].get("items", [])}
        dst_items = {it["title"].lower(): it for it in versions[to_version]["snapshot"].get("items", [])}
        added = [it for k, it in dst_items.items() if k not in src_items]
        removed = [it for k, it in src_items.items() if k not in dst_items]
        changed = [
            {"title": dst_items[k]["title"], "from": src_items[k]["content"], "to": dst_items[k]["content"]}
            for k in src_items.keys() & dst_items.keys()
            if src_items[k]["content"] != dst_items[k]["content"]
        ]
        compatible = len(removed) == 0 and len(changed) == 0
        self._audit(character_id, "canon_migrate_check", actor_brand, {
            "timeline_id": timeline_id, "from_version": from_version, "to_version": to_version,
            "compatible": compatible, "changed": len(changed), "removed": len(removed),
        })
        return {
            "character_id": character_id,
            "timeline_id": timeline_id,
            "from_version": from_version,
            "to_version": to_version,
            "direction": "rollback" if to_version < from_version else "upgrade",
            "compatible": compatible,
            "added": added,
            "removed": removed,
            "changed": changed,
            "target_snapshot_sha256": versions[to_version]["snapshot_sha256"],
        }

    # -- K4 / Contract 1: Context API, Hybrid Retrieval & Rights Verification ----

    def query_context(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        """Contract 1: Two-level authorized Context API with hybrid search, filters, budget,
        provenance, editorial live view vs immutable Job snapshot pinning."""
        self._require_read_access(character_id, actor_brand, actor_role)
        self._ensure_main_timeline(character_id)

        profile_name = str(payload.get("profile") or "script_agent").strip().lower()
        if profile_name not in CONTEXT_PROFILES:
            raise KnowledgeError(f"Unknown context profile: {profile_name}", 422)
        profile_cfg = CONTEXT_PROFILES[profile_name]

        mode = str(payload.get("mode") or "editorial").strip().lower()
        if mode not in {"editorial", "job"}:
            raise KnowledgeError("mode must be 'editorial' or 'job'", 422)
        job_id = str(payload.get("job_id") or "").strip() or None
        if mode == "job" and not job_id:
            raise KnowledgeError("job_id is required when mode='job'", 422)

        timeline_id = str(payload.get("timeline_id") or "main").strip()
        max_timeline_order = payload.get("max_timeline_order")
        if max_timeline_order is not None:
            max_timeline_order = int(max_timeline_order)
        query_text = str(payload.get("query") or "").strip()
        query_embedding = payload.get("query_embedding") if isinstance(payload.get("query_embedding"), list) else None
        requested_layers = payload.get("layers") or profile_cfg["layers"]
        allowed_layers = [l for l in requested_layers if l in profile_cfg["layers"]]
        budget_tokens = min(
            int(payload.get("budget_tokens") or profile_cfg["default_budget_tokens"]),
            profile_cfg["max_budget_tokens"],
        )
        min_score = float(payload.get("min_score", 0.12))
        include_shared_characters = bool(
            payload.get("include_shared_characters", payload.get("include_shared", False))
        )

        # Check cache for editorial reads (revocation invalidates cache synchronously).
        cache_key = f"{character_id}:{profile_name}:{timeline_id}:{max_timeline_order}:{query_text}:{budget_tokens}:{','.join(allowed_layers)}:{include_shared_characters}"
        if mode == "editorial" and query_embedding is None:
            with _CACHE_LOCK:
                cached = _CONTEXT_CACHE.get(cache_key)
                if cached is not None:
                    return dict(cached)

        # Determine which characters' approved knowledge can be read (character isolation + controlled sharing).
        readable_chars = [character_id]
        if include_shared_characters:
            policy = self.get_policy(character_id)
            for other_id in policy["shared_with_characters"]:
                other_policy = self.get_policy(str(other_id))
                if character_id in other_policy["shared_with_characters"]:
                    readable_chars.append(str(other_id))

        # Collect eligible timelines: alternative timelines inherit parent items up to branch_point_order,
        # while 'main' NEVER sees alternative timeline items!
        timeline_scope: list[tuple[str, int | None]] = [(timeline_id, max_timeline_order)]
        if timeline_id != "main":
            t_rows = cl.query(
                "SELECT parent_timeline_id, branch_point_order FROM character_timelines "
                "WHERE character_id = %s AND timeline_id = %s",
                (character_id, timeline_id),
            )
            if not t_rows:
                raise KnowledgeError(f"Timeline {timeline_id!r} not found", 404)
            parent_id, branch_point = t_rows[0]
            if parent_id:
                cap = int(branch_point or 0)
                if max_timeline_order is not None:
                    cap = min(cap, max_timeline_order)
                timeline_scope.append((parent_id, cap))

        # Load only APPROVED items whose backing source (if any) still has VERIFIED_RIGHTS!
        candidates: list[dict[str, Any]] = []
        for cid in readable_chars:
            for tl_id, tl_cap in timeline_scope:
                rows = cl.query(
                    "SELECT i.item_id, i.character_id, i.source_id, i.source_version, i.page_number, "
                    "i.layer, i.item_type, i.truth_status, i.title, i.content, i.provenance, "
                    "i.confidence, i.risk_level, i.status, i.version, i.timeline_id, i.timeline_order, "
                    "i.scene_id, i.job_id, i.safety_flags, i.approved_by, i.approved_at, "
                    "i.revoked_by, i.revoked_at, i.revocation_reason, i.legal_hold, i.created_by, "
                    "i.created_at, i.updated_at, s.rights_status "
                    "FROM character_knowledge_items i "
                    "LEFT JOIN character_knowledge_sources s "
                    "  ON s.source_id = i.source_id AND s.character_id = i.character_id "
                    "WHERE i.character_id = %s AND i.status = 'approved' AND i.timeline_id = %s",
                    (cid, tl_id),
                )
                for row in rows:
                    source_rights = row[29]
                    if row[2] is not None and source_rights not in VERIFIED_RIGHTS:
                        continue  # Fail-closed: source unverified, quarantined, or revoked!
                    item = self._row_to_item(row[:29])
                    if item["layer"] not in allowed_layers:
                        continue
                    if tl_cap is not None and item["timeline_order"] > tl_cap:
                        continue
                    candidates.append(item)

        # Hybrid scoring (lexical token/stem overlap + trigram Jaccard + optional vector cosine + layer priority).
        q_tokens = set(_tokens(query_text))
        q_trigrams = _trigrams(query_text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            haystack = f"{item['title']} {item['content']}"
            if not query_text:
                score = 0.5 + 0.1 * LAYER_PRIORITY.get(item["layer"], 0)
            else:
                i_tokens = set(_tokens(haystack))
                tok_overlap = len(q_tokens & i_tokens) / max(1, len(q_tokens)) if q_tokens else 0.0
                i_trigrams = _trigrams(haystack)
                tri_overlap = len(q_trigrams & i_trigrams) / max(1, len(q_trigrams)) if q_trigrams else 0.0
                lexical = 0.7 * tok_overlap + 0.3 * tri_overlap
                vec_score = 0.0
                item_emb = item["provenance"].get("embedding")
                if query_embedding and isinstance(item_emb, list):
                    vec_score = _cosine(query_embedding, item_emb)
                score = max(lexical, 0.5 * lexical + 0.5 * vec_score)
                # Always keep core canon traits lightly boosted when relevant, but respect threshold for query!
                if item["layer"] == "canon" and score >= min_score:
                    score += 0.15
                if score < min_score:
                    continue
            scored.append((round(score, 4), item))

        # Sort by layer precedence (canon -> memory -> knowledge -> belief) combined with relevance score.
        scored.sort(key=lambda pair: (LAYER_PRIORITY.get(pair[1]["layer"], 0), pair[0]), reverse=True)

        # Apply strict token budget.
        selected: list[dict[str, Any]] = []
        used_tokens = 0
        truncated = False
        for score, item in scored:
            cost = _estimate_tokens(f"{item['title']}: {item['content']}")
            if used_tokens + cost > budget_tokens:
                truncated = True
                continue
            used_tokens += cost
            selected.append({
                "item_id": item["item_id"],
                "character_id": item["character_id"],
                "shared_from_character": item["character_id"] if item["character_id"] != character_id else None,
                "layer": item["layer"],
                "item_type": item["item_type"],
                "truth_status": item["truth_status"],
                "title": item["title"],
                "content": item["content"],
                "version": item["version"],
                "timeline_id": item["timeline_id"],
                "timeline_order": item["timeline_order"],
                "score": score,
                "provenance": item["provenance"],
            })

        # Latest published canon version on this timeline (if any).
        canon_rows = cl.query(
            "SELECT MAX(canon_version) FROM character_canon_versions WHERE character_id = %s AND timeline_id = %s",
            (character_id, timeline_id),
        )
        canon_version = int(canon_rows[0][0]) if canon_rows and canon_rows[0][0] is not None else None

        snapshot_info: dict[str, Any] | None = None
        if mode == "job" and (selected or canon_version is not None or payload.get("force_snapshot")):
            item_refs = [
                {"item_id": it["item_id"], "character_id": it["character_id"],
                 "version": it["version"], "source_id": it["provenance"].get("source_id"),
                 "source_version": it["provenance"].get("source_version")}
                for it in selected
            ]
            source_refs = sorted({
                (it["provenance"]["source_id"], it["provenance"].get("source_version", 1))
                for it in selected if it["provenance"].get("source_id")
            })
            source_ref_dicts = [{"source_id": sid, "source_version": sv} for sid, sv in source_refs]
            snapshot_id = f"ksnap-{uuid.uuid4().hex[:12]}"
            digest = _canonical_sha256({
                "character_id": character_id, "job_id": job_id, "profile": profile_name,
                "timeline_id": timeline_id, "canon_version": canon_version,
                "item_refs": item_refs, "source_refs": source_ref_dicts,
            })
            cl.execute(
                "INSERT INTO character_knowledge_snapshots "
                "(snapshot_id, character_id, job_id, brand_id, profile, timeline_id, canon_version, "
                "item_refs, source_refs, snapshot_sha256, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
                (
                    snapshot_id, character_id, job_id, actor_brand or "core", profile_name,
                    timeline_id, canon_version,
                    json.dumps(item_refs, ensure_ascii=False),
                    json.dumps(source_ref_dicts, ensure_ascii=False),
                    digest,
                ),
            )
            snapshot_info = {
                "snapshot_id": snapshot_id,
                "snapshot_sha256": digest,
                "canon_version": canon_version,
                "timeline_id": timeline_id,
                "item_refs": item_refs,
                "source_refs": source_ref_dicts,
            }

        response = {
            "character_id": character_id,
            "profile": profile_name,
            "mode": mode,
            "timeline_id": timeline_id,
            "canon_version": canon_version,
            "budget_tokens": budget_tokens,
            "used_tokens": used_tokens,
            "truncated": truncated,
            "no_grounded_sources": len(selected) == 0,
            "items": selected,
            "snapshot": snapshot_info,
        }
        if mode == "editorial" and query_embedding is None:
            with _CACHE_LOCK:
                _CONTEXT_CACHE[cache_key] = dict(response)
        return response

    def get_snapshot(
        self,
        character_id: str,
        snapshot_id: str,
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        self._require_read_access(character_id, actor_brand, actor_role)
        rows = cl.query(
            "SELECT snapshot_id, character_id, job_id, brand_id, profile, timeline_id, canon_version, "
            "item_refs, source_refs, snapshot_sha256, created_at "
            "FROM character_knowledge_snapshots WHERE character_id = %s AND snapshot_id = %s",
            (character_id, snapshot_id),
        )
        if not rows:
            raise KnowledgeError(f"Snapshot {snapshot_id!r} not found", 404)
        r = rows[0]
        return {
            "snapshot_id": r[0], "character_id": r[1], "job_id": r[2], "brand_id": r[3],
            "profile": r[4], "timeline_id": r[5], "canon_version": r[6],
            "item_refs": _json_val(r[7], []), "source_refs": _json_val(r[8], []),
            "snapshot_sha256": r[9],
            "created_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]),
        }

    def verify_rights(
        self,
        character_id: str,
        payload: dict[str, Any],
        *,
        actor_brand: str | None,
        actor_role: str,
    ) -> dict[str, Any]:
        """Verify current permissions for re-execution ('rerun') or pre-publication ('publish').

        Final clarifications (#506):
        - 'rerun': ANY revoked/blocked/deleted item or unverified source denies re-execution.
        - 'publish': a previously approved result may still be published unless an active
          legal_block or critical_block applies to any referenced source/item.
        """
        self._require_read_access(character_id, actor_brand, actor_role)
        purpose = str(payload.get("purpose") or "rerun").strip().lower()
        if purpose not in {"rerun", "publish"}:
            raise KnowledgeError("purpose must be 'rerun' or 'publish'", 422)

        snapshot_id = payload.get("snapshot_id")
        item_ids = list(payload.get("item_ids") or [])
        source_ids = list(payload.get("source_ids") or [])
        if snapshot_id:
            snap = self.get_snapshot(character_id, str(snapshot_id), actor_brand=actor_brand, actor_role=actor_role)
            item_ids.extend(ref["item_id"] for ref in snap["item_refs"] if ref.get("item_id"))
            source_ids.extend(ref["source_id"] for ref in snap["source_refs"] if ref.get("source_id"))

        item_ids = sorted(set(str(x) for x in item_ids))
        source_ids = sorted(set(str(x) for x in source_ids))

        revoked_items: list[dict[str, Any]] = []
        legal_or_critical_blocks: list[dict[str, Any]] = []

        for iid in item_ids:
            rows = cl.query(
                "SELECT status, legal_hold, revocation_reason FROM character_knowledge_items "
                "WHERE character_id = %s AND item_id = %s",
                (character_id, iid),
            )
            if not rows:
                revoked_items.append({"item_id": iid, "reason": "missing"})
                continue
            status, legal_hold, reason = rows[0]
            if status != "approved":
                revoked_items.append({"item_id": iid, "status": status, "reason": reason})
            if legal_hold:
                legal_or_critical_blocks.append({"target_type": "item", "target_id": iid, "reason": reason})

        for sid in source_ids:
            rows = cl.query(
                "SELECT rights_status, quarantine_reason FROM character_knowledge_sources "
                "WHERE character_id = %s AND source_id = %s",
                (character_id, sid),
            )
            if not rows:
                revoked_items.append({"source_id": sid, "reason": "missing"})
                continue
            r_status, q_reason = rows[0]
            if r_status not in VERIFIED_RIGHTS:
                revoked_items.append({"source_id": sid, "rights_status": r_status, "reason": q_reason})
            rev_rows = cl.query(
                "SELECT action, legal_block, critical_block, reason FROM character_knowledge_revocations "
                "WHERE character_id = %s AND target_type = 'source' AND target_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (character_id, sid),
            )
            if rev_rows:
                act, leg, crit, rev_reason = rev_rows[0]
                if act in {"block", "revoke", "delete"} and (leg or crit):
                    legal_or_critical_blocks.append({
                        "target_type": "source", "target_id": sid, "legal_block": bool(leg),
                        "critical_block": bool(crit), "reason": rev_reason,
                    })

        # Also check explicit item-level revocations for legal/critical flags.
        for iid in item_ids:
            rev_rows = cl.query(
                "SELECT action, legal_block, critical_block, reason FROM character_knowledge_revocations "
                "WHERE character_id = %s AND target_type = 'item' AND target_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (character_id, iid),
            )
            if rev_rows:
                act, leg, crit, rev_reason = rev_rows[0]
                if act in {"block", "revoke", "delete"} and (leg or crit):
                    if not any(b.get("target_id") == iid for b in legal_or_critical_blocks):
                        legal_or_critical_blocks.append({
                            "target_type": "item", "target_id": iid, "legal_block": bool(leg),
                            "critical_block": bool(crit), "reason": rev_reason,
                        })

        if purpose == "publish":
            allowed = len(legal_or_critical_blocks) == 0
        else:
            allowed = len(revoked_items) == 0 and len(legal_or_critical_blocks) == 0

        return {
            "character_id": character_id,
            "purpose": purpose,
            "allowed": allowed,
            "revoked_items": revoked_items,
            "legal_or_critical_blocks": legal_or_critical_blocks,
        }
