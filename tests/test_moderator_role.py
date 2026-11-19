"""Owner decision 19.11.2026 (#499/#500/#502): two access roles, admin and a brand
moderator; the former moderator role is gone (stored ``moderator`` = moderator, no brands)."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient


def _client(monkeypatch, user, role, brands=None):
    from core.app import app
    from core.security import _session_token, session_secret
    record = {"password_hash": "x", "role": role}
    if brands is not None:
        record["brands"] = brands
    monkeypatch.setenv("USERS_JSON", json.dumps({user: record}))
    session = _session_token(user, role)
    secret = session_secret()
    csrf = hmac.new(secret.encode() if isinstance(secret, str) else secret, session.encode(), hashlib.sha256).hexdigest()
    client = TestClient(app, headers={"Authorization": "", "x-csrf-token": csrf})
    client.cookies.set("vertep_session", session)
    client.cookies.set("vertep_csrf", csrf)
    return client


def test_roles_are_admin_and_moderator_and_a_stored_moderator_is_a_moderator_without_brands(monkeypatch):
    from core.security import SESSION_ROLES, _normalize_role, user_brands
    assert SESSION_ROLES == ("admin", "moderator")
    assert _normalize_role("moderator") == "moderator"
    monkeypatch.setenv("USERS_JSON", json.dumps({"old": {"role": "moderator"}, "mod": {"role": "moderator", "brands": ["brand-a"]}}))
    assert user_brands("old", "moderator") == set()
    assert user_brands("mod", "moderator") == {"brand-a"}
    assert user_brands("root", "admin") is None


def test_moderator_creates_jobs_only_for_its_brands_and_no_admin_actions(monkeypatch):
    moderator = _client(monkeypatch, "mod", "moderator", ["brand-a"])
    assert moderator.get("/api/session").json()["role"] == "moderator"
    foreign = moderator.post("/api/jobs", json={"topic": "Тест", "brand_id": "brand-b"})
    assert foreign.status_code == 403 and foreign.json()["detail"] == "Brand access required"
    own = moderator.post("/api/jobs", json={"topic": "Тест", "brand_id": "brand-a"})
    assert own.status_code != 403, own.text  # brand passes; character/configuration decide the rest
    for method, path in (("put", "/api/settings/providers"), ("post", "/api/nodes/registration-tokens"),
                         ("post", "/api/system/backups"), ("delete", "/api/brands/brand-a")):
        response = moderator.request(method.upper(), path, json={})
        assert response.status_code == 403, path
    former_moderator = _client(monkeypatch, "old", "moderator")
    assert former_moderator.post("/api/jobs", json={"topic": "Тест", "brand_id": "brand-a"}).status_code == 403


def test_characters_module_accepts_a_moderator_only_for_its_brand(monkeypatch):
    monkeypatch.setenv("VERTEP_CORE_URL", "http://core.invalid")
    monkeypatch.setenv("VERTEP_MODULE_TOKEN", "module-token")
    from modules.characters.characters_module import _may_change
    moderator = {"x-vertep-role": "moderator", "x-vertep-brands": "brand-a,brand-c"}
    assert _may_change(moderator, "brand-a") is True
    assert _may_change(moderator, "brand-b") is False
    assert _may_change(moderator, None) is False
    assert _may_change({"x-vertep-role": "admin", "x-vertep-brands": "*"}, "brand-b") is True
    assert _may_change({"x-vertep-brands": "brand-a"}, "brand-a") is False  # no role from the gateway


def test_session_reports_the_moderator_brands_and_job_actions_follow_the_job_brand(monkeypatch):
    from core.models import JobStatus
    from core.state import store
    moderator = _client(monkeypatch, "mod", "moderator", ["brand-a"])
    session = moderator.get("/api/session").json()
    assert session["role"] == "moderator" and session["brands"] == ["brand-a"]
    job_a = store.create("Тест A", "character-x", 1, "api", "image", 0, "brand-a")
    job_b = store.create("Тест B", "character-x", 1, "api", "image", 0, "brand-b")
    for job in (job_a, job_b):
        store.update(job, JobStatus.READY, "READY (test)")
    own = moderator.post(f"/api/jobs/{job_a.job_id}/pause")
    assert own.status_code != 403, own.text  # own brand: the endpoint decides by state
    foreign = moderator.post(f"/api/jobs/{job_b.job_id}/pause")
    assert foreign.status_code == 403
    assert moderator.request("DELETE", f"/api/jobs/{job_a.job_id}").status_code == 403  # deletion stays admin-only
