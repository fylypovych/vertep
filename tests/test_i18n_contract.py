"""i.0.0.4.33/34/35: dictionaries of the panel and machine codes of API errors."""
import ast
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.error_codes import ERROR_CODES, error_code, install_error_codes

APP = Path("web-v2/src/app")


def _wrapped() -> set[str]:
    found = set()
    for path in [*APP.rglob("*.ts"), *APP.rglob("*.html")]:
        for text in re.findall(r"\{\{ '((?:[^'\\]|\\.)*)' \| t \}\}", path.read_text(encoding="utf-8").replace("\\\\'", "\\'")):
            found.add(text.replace("\\'", "'"))
    return found


def _english() -> dict[str, str]:
    source = (APP / "core/i18n/en.ts").read_text(encoding="utf-8")
    return json.loads(source[source.index("= {") + 2:source.rindex("}") + 1])


def test_every_wrapped_text_has_an_english_translation():
    english = _english()
    missing = sorted(_wrapped() - set(english))
    assert not missing, missing[:20]
    assert all(value.strip() and not re.search("[А-Яа-яЄєІіЇї]", value) for value in english.values())


def test_every_ukrainian_literal_of_the_panel_has_an_english_translation():
    english = _english()
    found = set()
    for path in [*APP.rglob("*.ts"), *APP.rglob("*.html")]:
        if path.name in {"en.ts", "api-errors.ts", "uk.pipe.ts", "language-switch.component.ts"}:
            continue
        source = re.sub(r"<!--.*?-->|/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S).replace("\\\\'", "\\'")
        source = re.sub(r"//[^\n'\"`]*[А-Яа-яЄєІіЇї][^\n]*", "", source)
        for text in re.findall(r"'((?:[^'\\\n]|\\.)*)'", source):
            if re.search("[А-Яа-яЄєІіЇї]", text):
                found.add(text.replace("\\'", "'").replace("\\n", "\n"))
    missing = sorted(found - set(english))
    assert not missing, missing[:20]


def test_every_api_error_message_has_a_code():
    known = set(ERROR_CODES)
    missing = []
    for path in Path("core").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "name", "")) == "HTTPException":
                detail = node.args[1] if len(node.args) > 1 else next(
                    (item.value for item in node.keywords if item.arg == "detail"), None)
                if isinstance(detail, ast.Constant) and isinstance(detail.value, str) and detail.value not in known:
                    missing.append(detail.value)
    assert not missing, missing


def test_every_code_has_text_in_both_languages():
    source = (APP / "core/i18n/api-errors.ts").read_text(encoding="utf-8")
    rows = dict(re.findall(r'^  "([a-z0-9_]+)": \{ uk: "(.+?)", en: "', source, flags=re.M))
    assert set(ERROR_CODES.values()) == set(rows)
    assert len(re.findall(r'en: "[^"]+"', source)) == len(rows)


def test_response_keeps_detail_and_adds_the_code():
    app = FastAPI()
    install_error_codes(app)

    @app.get("/known")
    def known():
        raise HTTPException(404, "Job not found", headers={"X-Test": "1"})

    @app.get("/built")
    def built():
        raise HTTPException(409, "Job 5 is busy")

    client = TestClient(app)
    answer = client.get("/known")
    assert answer.status_code == 404 and answer.headers["X-Test"] == "1"
    assert answer.json() == {"detail": "Job not found", "code": "job_not_found"}
    assert client.get("/built").json() == {"detail": "Job 5 is busy", "code": "http_409"}
    assert client.get("/absent").json()["code"] == "http_404"


def test_structured_detail_gets_a_status_code():
    assert error_code({"message": "x"}, 423) == "http_423"
