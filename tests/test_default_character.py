"""i.0.0.4.75 (#475): the default character comes from configuration, never from code."""

import io
import json
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.telegram.job_callbacks as callbacks
from core.app import app
from core.configuration import DefaultCharacterError, default_character_id
from core.state import store

ROOT = Path(__file__).resolve().parent.parent
OTHER = "other-char"


@pytest.fixture
def characters(monkeypatch, tmp_path):
    """A catalog with the stock character and another one, without any default."""
    root = tmp_path / "characters"
    shutil.copytree(ROOT / "characters/did_samogon", root / "did_samogon")
    shutil.copytree(ROOT / "characters/did_samogon", root / OTHER)
    config = json.loads((root / OTHER / "character.json").read_text(encoding="utf-8"))
    config.update(id=OTHER, name="Other")
    (root / OTHER / "character.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("CHARACTERS_ROOT", str(root))
    monkeypatch.delenv("DEFAULT_CHARACTER", raising=False)
    monkeypatch.delenv("TELEGRAM_DEFAULT_CHARACTER", raising=False)
    return root


def test_runtime_code_has_no_hardcoded_character_id():
    hits = []
    for folder in ("core", "adapters", "worker", "services", "scripts", "installer"):
        for path in (ROOT / folder).rglob("*.py"):
            if "did_samogon" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
    assert hits == []


class TestResolver:
    def test_configured_default(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", OTHER)
        assert default_character_id() == OTHER

    def test_missing_default_is_an_explicit_error(self, characters):
        with pytest.raises(DefaultCharacterError, match="No default character is configured"):
            default_character_id()

    @pytest.mark.parametrize("value", ["absent-char", "../did_samogon", "Bad Id"])
    def test_invalid_default_is_an_error_not_another_character(self, characters, monkeypatch, value):
        monkeypatch.setenv("DEFAULT_CHARACTER", value)
        with pytest.raises(DefaultCharacterError, match="does not exist"):
            default_character_id()

    def test_specific_setting_wins_and_an_invalid_one_does_not_fall_through(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", "did_samogon")
        monkeypatch.setenv("TELEGRAM_DEFAULT_CHARACTER", OTHER)
        assert default_character_id("TELEGRAM_DEFAULT_CHARACTER") == OTHER
        monkeypatch.setenv("TELEGRAM_DEFAULT_CHARACTER", "absent-char")
        with pytest.raises(DefaultCharacterError):
            default_character_id("TELEGRAM_DEFAULT_CHARACTER")
        monkeypatch.setenv("TELEGRAM_DEFAULT_CHARACTER", "")
        assert default_character_id("TELEGRAM_DEFAULT_CHARACTER") == "did_samogon"


class TestApi:
    def test_omitted_character_uses_the_configured_default(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", OTHER)
        response = TestClient(app).post("/api/jobs", json={"topic": "з налаштованим default"})
        assert response.status_code == 200, response.text
        assert response.json()["character_id"] == OTHER

    def test_omitted_character_without_default_is_422(self, characters):
        before = set(store.jobs)
        response = TestClient(app).post("/api/jobs", json={"topic": "без default"})
        assert response.status_code == 422
        assert "DEFAULT_CHARACTER" in response.json()["detail"]
        assert set(store.jobs) == before

    def test_invalid_default_is_422(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", "absent-char")
        assert TestClient(app).post("/api/jobs", json={"topic": "x"}).status_code == 422

    def test_explicit_selection_needs_no_default(self, characters):
        response = TestClient(app).post("/api/jobs", json={"topic": "явний", "character_id": OTHER})
        assert response.status_code == 200, response.text
        assert response.json()["character_id"] == OTHER


def _archive(job: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("job.json", json.dumps(job))
    return buffer.getvalue()


class TestImport:
    def _import(self, job):
        return TestClient(app).post("/api/projects/import", content=_archive(job),
                                    headers={"content-type": "application/zip"})

    def test_persisted_character_is_kept(self, characters):
        response = self._import({"topic": "старий", "character_id": OTHER, "priority": 5})
        assert response.status_code == 200, response.text
        assert response.json()["character_id"] == OTHER

    def test_missing_character_uses_the_configured_default(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", OTHER)
        response = self._import({"topic": "без персонажа", "priority": 5})
        assert response.status_code == 200, response.text
        assert response.json()["character_id"] == OTHER

    def test_missing_character_without_default_is_422(self, characters):
        response = self._import({"topic": "без персонажа", "priority": 5})
        assert response.status_code == 422


class TestTelegram:
    PENDING = {"text": "тема", "source_id": None, "message": {"chat": {"id": 4242}}}

    def test_telegram_default_comes_from_configuration(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", "did_samogon")
        monkeypatch.setenv("TELEGRAM_DEFAULT_CHARACTER", OTHER)
        job = callbacks._create_job_from_telegram(dict(self.PENDING, source_id="tg-1"), "brand01")
        assert job.character_id == OTHER

    def test_telegram_falls_back_to_the_general_default(self, characters, monkeypatch):
        monkeypatch.setenv("DEFAULT_CHARACTER", OTHER)
        job = callbacks._create_job_from_telegram(dict(self.PENDING, source_id="tg-2"), "brand01")
        assert job.character_id == OTHER

    def test_telegram_without_default_refuses(self, characters):
        with pytest.raises(HTTPException) as error:
            callbacks._create_job_from_telegram(dict(self.PENDING, source_id="tg-3"), "brand01")
        assert error.value.status_code == 422

    def test_telegram_explicit_selection_is_kept(self, characters):
        job = callbacks._create_job_from_telegram(dict(self.PENDING, source_id="tg-4"), "brand01", OTHER)
        assert job.character_id == OTHER

    def test_empty_catalog_without_default_sends_an_error_and_creates_nothing(self, monkeypatch, tmp_path):
        empty = tmp_path / "none"
        empty.mkdir()
        monkeypatch.setenv("CHARACTERS_ROOT", str(empty))
        monkeypatch.delenv("DEFAULT_CHARACTER", raising=False)
        monkeypatch.delenv("TELEGRAM_DEFAULT_CHARACTER", raising=False)
        sent = []
        monkeypatch.setattr(callbacks.TelegramAdapter, "send_message",
                            lambda self, chat, text, *a, **k: sent.append(text) or {"ok": True})
        callbacks._telegram_pending_character["777"] = {"pending": self.PENDING, "brand_id": "brand01"}
        before = set(store.jobs)
        try:
            callbacks._send_character_selection("777", "brand01")
        finally:
            callbacks._telegram_pending_character.pop("777", None)
        assert set(store.jobs) == before
        assert sent and re.search("персонажа", sent[-1])


def test_bootstrap_passes_a_provided_default_character_without_hardcoding_one():
    """audit-1511 В5: automation may set DEFAULT_CHARACTER; bootstrap names no character."""
    bootstrap = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "${DEFAULT_CHARACTER:+DEFAULT_CHARACTER=$DEFAULT_CHARACTER}" in bootstrap
    assert "did_samogon" not in bootstrap
