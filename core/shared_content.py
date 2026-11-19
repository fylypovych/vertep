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
    """All brands other than the job's own whose characters the material uses."""
    owners = set(getattr(job, "character_owner_brands", None) or [])
    if getattr(job, "character_owner_brand", None):
        owners.add(job.character_owner_brand)
    owners.discard(job.brand_id)
    return sorted(owners)


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
    source = getattr(job, "shared_source", None)
    if source:
        return _derived_allowed(job, source, root)
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


def _derived_allowed(job: Job, source: dict, root: Path) -> tuple[bool, str | None]:
    """An owner's own publication follows the source material's decisions: the
    other owners' approvals of that exact version, revocation and the hash."""
    from .pipeline import _digest
    from .state import store
    original = store.jobs.get(source.get("job_id"))
    if original is None:
        return False, "Source material is no longer available"
    active = _active_version(original)
    if active is None or active.version != source.get("version") or active.sha256 != source.get("sha256"):
        return False, "Source material changed; a new approval is needed"
    current = state(original)["owners"]
    missing = [owner for owner in foreign_owners(original) if current.get(owner) != "approved"]
    if missing:
        return False, f"Shared content needs approval of brand(s): {', '.join(missing)}"
    mine = _active_version(job)
    path = Path(root) / job.job_id / mine.path if mine else None
    if mine is None or mine.sha256 != source.get("sha256") or not path.is_file() or _digest(path) != mine.sha256:
        return False, "Approved video file does not match its approved hash"
    return True, None


def create_owner_publication(store, source_job: Job, brand: str, actor: str, metadata: dict) -> Job:
    """The owner brand publishes the same approved artifact on its own: a new job of
    that brand carries the identical file (version + sha256) and its own metadata.
    Callers hold the store lock."""
    import shutil
    from .models import JobStatus, VideoVersion
    if brand not in foreign_owners(source_job):
        raise SharedContentError("Only a foreign owner brand may publish this material independently")
    allowed, reason = publication_allowed(source_job, store.root)
    if not allowed:
        raise SharedContentError(reason or "Material is not approved")
    active = _active_version(source_job)
    if active is None or not active.approved:
        raise SharedContentError("The material's own brand has not approved this version")
    clean = {key: metadata[key] for key in ("title", "description", "hashtags", "cover") if metadata.get(key) is not None}
    if "hashtags" in clean and (not isinstance(clean["hashtags"], list)
                                or any(not isinstance(tag, str) for tag in clean["hashtags"])):
        raise SharedContentError("hashtags must be a list of strings")
    title = str(clean.get("title") or source_job.topic)[:500]
    derived = store.create(title, source_job.character_id, source_job.priority, "shared-content",
                           source_job.task_type, 0, brand, source_job.workflow,
                           source_job.aspect_ratio, source_job.output_preset)
    target = store.root / derived.job_id / active.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(store.root / source_job.job_id / active.path, target)
    derived.video_versions = [VideoVersion(version=active.version, path=active.path, sha256=active.sha256,
                                           approved=True, approved_by=actor, approved_at=utc_now())]
    derived.active_video_version = active.version
    derived.output_path = str(target)
    derived.character_owner_brand = source_job.character_owner_brand
    derived.character_owner_brands = list(source_job.character_owner_brands)
    derived.shared_source = {"job_id": source_job.job_id, "version": active.version, "sha256": active.sha256}
    derived.publication_metadata = clean
    record = {"brand": brand, "job_id": derived.job_id, "version": active.version, "sha256": active.sha256,
              "actor": actor, "at": utc_now(), "metadata": clean}
    source_job.shared_publications.append(record)
    store.update(derived, JobStatus.READY, f"SHARED PUBLICATION OF {source_job.job_id} v{active.version} BY {brand} ({actor})")
    store.update(source_job, source_job.status, f"SHARED PUBLICATION {derived.job_id} CREATED BY {brand} ({actor})")
    return derived
