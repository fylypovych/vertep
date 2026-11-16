"""i.0.0.4.76 (#476): an incomplete character is never announced as ready (§3.2)."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import core.telegram.job_callbacks as callbacks
from core.app import app
from core.configuration import (REQUIRED_CHARACTER_FILES, DefaultCharacterError,
                                IncompleteCharacterError, default_character_id, load_character,
                                missing_character_files)

ROOT = Path(__file__).resolve().parent.parent


def _partial(root: Path, character_id: str, files=("character.json", "system_prompt.txt", "voice.json")):
    directory = root / character_id
    directory.mkdir(parents=True)
    for name in files:
        content = (json.dumps({"id": character_id, "name": "Partial"}) if name == "character.json"
                   else "" if name.endswith(".txt") else "{}")
        (directory / name).write_text(content, encoding="utf-8")


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    root = tmp_path / "characters"
    shutil.copytree(ROOT / "characters/did_samogon", root / "did_samogon")
    _partial(root, "partial-char")
    monkeypatch.setenv("CHARACTERS_ROOT", str(root))
    monkeypatch.delenv("TELEGRAM_DEFAULT_CHARACTER", raising=False)
    return root


@pytest.mark.parametrize("absent", REQUIRED_CHARACTER_FILES)
def test_each_required_file_is_detected(tmp_path, absent):
    _partial(tmp_path, "one-missing", [name for name in REQUIRED_CHARACTER_FILES if name != absent])
    assert missing_character_files(tmp_path, "one-missing") == [absent]


def test_valid_catalog_character_is_complete():
    assert missing_character_files(ROOT / "characters", "did_samogon") == []
    load_character(ROOT / "characters", "did_samogon", require_complete=True)


def test_strict_load_names_the_missing_files(catalog):
    with pytest.raises(IncompleteCharacterError, match="visual.json, generation.json, publishing.json"):
        load_character(catalog, "partial-char", require_complete=True)
    # An explicit id keeps loading as before: persisted jobs do not regress.
    assert load_character(catalog, "partial-char").id == "partial-char"


def test_listing_does_not_offer_an_incomplete_character(catalog):
    ids = [item["id"] for item in TestClient(app).get("/api/characters").json()]
    assert ids == ["did_samogon"]


def test_incomplete_default_is_refused(catalog, monkeypatch):
    monkeypatch.setenv("DEFAULT_CHARACTER", "partial-char")
    with pytest.raises(DefaultCharacterError, match="incomplete"):
        default_character_id()
    assert TestClient(app).post("/api/jobs", json={"topic": "x"}).status_code == 422


def test_telegram_does_not_offer_an_incomplete_character(catalog, monkeypatch):
    sent = []
    monkeypatch.setattr(callbacks.TelegramAdapter, "send_message",
                        lambda self, chat, text, keyboard=None, *a, **k: sent.append(keyboard) or {"ok": True})
    callbacks._send_character_selection("901", "brand01")
    buttons = [row[0]["callback_data"] for row in sent[-1]["inline_keyboard"]]
    assert buttons == ["select_character:did_samogon"]


def test_every_shipped_character_is_complete():
    """i.0.0.4.85: the catalog seeded into installations holds only complete characters;
    the incomplete test artefact char-contract was removed by the owner's decision."""
    shipped = sorted(path.name for path in (ROOT / "characters").iterdir() if path.is_dir())
    assert shipped and "char-contract" not in shipped
    assert {name: missing_character_files(ROOT / "characters", name) for name in shipped} == {
        name: [] for name in shipped}


def test_voice_issued_result_fixture_no_longer_writes_the_production_catalog():
    source = (ROOT / "tests/test_voice_issued_result.py").read_text(encoding="utf-8")
    assert 'characters_root="characters"' not in source
    assert 'characters_root: str = "characters"' not in source


# ── i.0.0.4.85 (#485): explicit ID of an incomplete character cannot start a new job ──

def test_api_refuses_a_new_job_for_an_incomplete_explicit_character(catalog):
    from core.state import store
    before = set(store.jobs)
    response = TestClient(app).post("/api/jobs", json={"topic": "x", "character_id": "partial-char"})
    assert response.status_code == 422
    assert "incomplete" in response.json()["detail"] and "visual.json" in response.json()["detail"]
    assert set(store.jobs) == before


def test_api_accepts_the_complete_control_character(catalog):
    response = TestClient(app).post("/api/jobs", json={"topic": "x", "character_id": "did_samogon"})
    assert response.status_code == 200, response.text
    assert response.json()["character_id"] == "did_samogon"


def test_api_unknown_character_keeps_its_previous_answer(catalog):
    assert TestClient(app).post("/api/jobs", json={"topic": "x", "character_id": "absent-char"}).status_code == 400


def test_telegram_refuses_an_incomplete_explicit_character(catalog):
    from fastapi import HTTPException
    pending = {"text": "тема", "source_id": "tg-485", "message": {"chat": {"id": 485}}}
    with pytest.raises(HTTPException) as error:
        callbacks._create_job_from_telegram(pending, "brand01", "partial-char")
    assert error.value.status_code == 422 and "incomplete" in str(error.value.detail)


def test_existing_job_of_an_incomplete_character_is_still_readable(catalog):
    """Reading a historical job does not declare readiness, so it stays permissive."""
    from core.state import store
    job = store.create(topic="history", character_id="partial-char", priority=5)
    try:
        response = TestClient(app).get(f"/api/jobs/{job.job_id}")
        assert response.status_code == 200
        assert response.json()["character_id"] == "partial-char"
        assert load_character(catalog, "partial-char").id == "partial-char"
    finally:
        store.delete(job.job_id)
