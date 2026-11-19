"""Shared content (#500): cross-brand approval of material that uses another
brand's character.

A job of brand A that uses a character owned by brand B records B as a foreign
owner (``Job.character_owner_brand``, snapshotted from the Characters module at
job creation). Brand A may generate before B decides; publishing the material
needs B's approval of the exact immutable video version (version + sha256).
Decisions are append-only records in ``Job.shared_approvals`` (actor, time,
decision, version, hash, brand), so history survives revisions, restarts and
migration. A pending decision never allows publication; it has no expiry.
Revocation blocks new, scheduled and not yet started sends; a send already
started finishes and already published material is not removed.
"""

from __future__ import annotations

from pathlib import Path

from .models import Job, utc_now

DECISIONS = {"approve", "reject"}


class SharedContentError(ValueError):
    pass


def foreign_owners(job: Job) -> list[str]:
    owner = getattr(job, "character_owner_brand", None)
    return [owner] if owner and owner != job.brand_id else []


def _active_version(job: Job):
    if job.active_video_version is None:
        return None
    return next((item for item in job.video_versions if item.version == job.active_video_version), None)


def _latest(job: Job, owner: str, version: int) -> dict | None:
    records = [item for item in job.shared_approvals if item.get("owner_brand") == owner and item.get("version") == version]
    return records[-1] if records else None


def state(job: Job) -> dict:
    """Per foreign owner: pending | approved | rejected | revoked for the active version."""
    active = _active_version(job)
    owners = {}
    for owner in foreign_owners(job):
        record = _latest(job, owner, active.version) if active else None
        if record is None:
            owners[owner] = "pending"
        elif record.get("decision") == "revoke":
            owners[owner] = "revoked"
        elif record.get("decision") == "approve" and record.get("sha256") == active.sha256:
            owners[owner] = "approved"
        elif record.get("decision") == "reject":
            owners[owner] = "rejected"
        else:
            owners[owner] = "pending"
    return {"job_id": job.job_id, "brand_id": job.brand_id, "character_id": job.character_id,
            "version": active.version if active else None, "sha256": active.sha256 if active else None,
            "owners": owners, "history": list(job.shared_approvals)}


def publication_allowed(job: Job, root: Path) -> tuple[bool, str | None]:
    """Atomic-boundary check (callers hold the store lock): every foreign owner
    approved the active version, the hash still matches the file on disk."""
    owners = foreign_owners(job)
    if not owners:
        return True, None
    active = _active_version(job)
    if active is None:
        return False, "No active video version"
    current = state(job)["owners"]
    missing = [owner for owner in owners if current.get(owner) != "approved"]
    if missing:
        return False, f"Shared content needs approval of brand(s): {', '.join(missing)}"
    from .pipeline import _digest
    path = Path(root) / job.job_id / active.path
    if not path.is_file() or _digest(path) != active.sha256:
        return False, "Approved video file does not match its approved hash"
    return True, None


def decide(job: Job, owner: str, decision: str, actor: str, *, version: int, sha256: str) -> dict:
    """Owner brand decides on the exact version it reviewed (callers hold the store lock)."""
    if owner not in foreign_owners(job):
        raise SharedContentError("Brand is not a foreign owner of this material")
    if decision not in DECISIONS:
        raise SharedContentError("Decision must be approve or reject")
    active = _active_version(job)
    if active is None or active.version != version or active.sha256 != sha256:
        raise SharedContentError("Stale decision: reviewed version or hash is not the active one")
    record = {"owner_brand": owner, "version": version, "sha256": sha256, "decision": decision,
              "actor": actor, "at": utc_now(), "job_brand": job.brand_id}
    job.shared_approvals.append(record)
    return record


def revoke(job: Job, owner: str, actor: str) -> dict:
    if owner not in foreign_owners(job):
        raise SharedContentError("Brand is not a foreign owner of this material")
    active = _active_version(job)
    if active is None or state(job)["owners"].get(owner) != "approved":
        raise SharedContentError("Nothing approved to revoke")
    record = {"owner_brand": owner, "version": active.version, "sha256": active.sha256, "decision": "revoke",
              "actor": actor, "at": utc_now(), "job_brand": job.brand_id}
    job.shared_approvals.append(record)
    return record
