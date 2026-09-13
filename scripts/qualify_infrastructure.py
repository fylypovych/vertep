#!/usr/bin/env python3
"""Automated infrastructure qualification harness for Vertep.

Відтворювана автоматизована кваліфікація infrastructure-сценаріїв Vertep без
вимоги доступу до реального production стенду (Issue i.0.0.0.31).

Harness лише *керує* виконанням: він збирає каталог сценаріїв, резолвить
автоматизовані pytest-цілі, виконує їх за потреби та формує єдиний формат
доказу результату: version/commit, scenario, environment, result/error.

Фактичне виконання на реальному обладнанні/розгортанні винесене в окремий
`rt.0.0.0.32` Issue; для таких сценаріїв тут реєструється поле "rt" з
посиланням на формалізовану executable-процедуру.
"""

from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
TOOL = "qualify-infrastructure"
ISSUE = "i.0.0.0.31"

# ---------------------------------------------------------------------------
# Каталог сценаріїв кваліфікації інфраструктури.
# Кожен елемент відповідає одній з 8 обов'язкових груп Issue i.0.0.0.31:
#   id, name        — ідентифікатор та назва сценарію;
#   automated       — pytest-цілі (шляхи відносно кореня репо), що реалізують
#                     сценарій автоматизовано;
#   rt              — формалізована executable test procedure для Real Test;
#   description     — короткий опис того, що кваліфікується.
# ---------------------------------------------------------------------------
SCENARIOS = [
    {
        "id": "S01",
        "name": "Clean Ubuntu-compatible bootstrap та First Run",
        "automated": ["tests/test_bootstrap_wizard.py", "tests/test_first_run.py"],
        "rt": "rt::S01 clean bootstrap + First Run Wizard",
        "description": "Automated/disposable qualification чистого Ubuntu-сумісного "
                       "bootstrap: roles, секрети, контейнери, health check.",
    },
    {
        "id": "S02",
        "name": "CORE + Worker enrollment, certificates, self-test, task/result, remote-control",
        "automated": ["tests/test_node_registry.py", "tests/test_fleet_controls.py", "tests/test_worker_self_test.py"],
        "rt": "rt::S02 фізичний multi-host enrollment + certificates",
        "description": "Реєстрація вузлів, сертифікати, self-test, task/result "
                       "та remote-control сценарії між CORE та Worker.",
    },
    {
        "id": "S03",
        "name": "GPU/ComfyUI-compatible integration harness",
        "automated": ["tests/test_core_generation_gate.py", "tests/test_compute_distributed.py"],
        "rt": "rt::S03 фізична GPU + ComfyUI workflow",
        "description": "Інтеграційний harness сумісний з GPU/ComfyUI без вимоги "
                       "фізичної GPU: генерація через mock/fallback, execution op.",
    },
    {
        "id": "S04",
        "name": "Backup → data mutation → restore → health verification",
        "automated": ["tests/test_role_services.py"],
        "rt": "rt::S04 реальний backup/restore стенд",
        "description": "Резервна копія, мутація даних, відновлення та перевірка "
                       "здоров'я у контрольованому середовищі.",
    },
    {
        "id": "S05",
        "name": "Existing-install migration/persistence harness",
        "automated": ["tests/test_persistent_user_data.py", "tests/test_deployment_plan.py"],
        "rt": "rt::S05 міграція існуючої інсталяції",
        "description": "characters/brands/workflows/jobs → update/recreate/"
                       "rollback/restore simulation без втрати даних.",
    },
    {
        "id": "S06",
        "name": "Safe Update fault-injection",
        "automated": ["tests/test_safe_update.py", "tests/test_rolling_update.py", "tests/test_updates.py"],
        "rt": "rt::S06 real update interruption/restart/rollback",
        "description": "rolling/canary, interruption/restart, rollback/recovery "
                       "з інжекцією відмов під час оновлення.",
    },
    {
        "id": "S07",
        "name": "Release trust automated checks",
        "automated": ["tests/test_key_lifecycle.py", "tests/test_update_security.py", "tests/test_release_qualification.py"],
        "rt": "rt::S07 key ceremony + recovery",
        "description": "signed artifacts, tamper/revoke/downgrade, key rotation "
                       "та recovery procedure.",
    },
    {
        "id": "S08",
        "name": "Publisher sandbox/fake integration з перевірюваним receipt",
        "automated": ["tests/test_publisher_live_adapters.py", "tests/test_publish_task_helpers.py"],
        "rt": "rt::S08 live platform publish receipt",
        "description": "Sandbox/fake publisher із перевірюваним receipt публікації.",
    },
]


def load_version() -> str:
    """Повертає поточну версію з `VERSION` або "unknown", якщо файл відсутній."""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
        return text or "unknown"
    except OSError:
        return "unknown"


def git_commit() -> str:
    """Повертає короткий SHA HEAD або "unknown"."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = result.stdout.strip()
        return out or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def environment() -> dict:
    """Опис environment, де виконується кваліфікація (version/commit доказ)."""
    return {
        "platform": sys.platform,
        "python": _platform.python_version(),
        "system": _platform.platform(),
        "is_windows": os.name == "nt",
        "runner": os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI") or "local",
    }


def collect_scenarios() -> list[dict]:
    """Резолвить кожен "automated" ціль у абсолютний шлях під коренем репо."""
    resolved = []
    for scenario in SCENARIOS:
        entry = dict(scenario)
        entry["automated"] = [_resolve(path) for path in scenario.get("automated", [])]
        resolved.append(entry)
    return resolved


def resolve_selected(all_scenarios: list[dict], selected: list[str]) -> list[dict]:
    """Фільтрує каталог сценаріїв за списком ідентифікаторів (S01, S03, ...)."""
    wanted = set(selected)
    return [scenario for scenario in all_scenarios if scenario["id"] in wanted]


def _resolve(target: str | Path) -> Path:
    path = Path(target)
    if path.is_absolute():
        return path
    return ROOT / path


def parse_pytest_summary(output: str) -> dict:
    """Витягує лічильники passed/failed/error з короткого звіту pytest -q."""
    def count(needle: str) -> int:
        match = re.search(r"(\d+)\s+" + re.escape(needle), output)
        return int(match.group(1)) if match else 0

    return {
        "passed": count("passed"),
        "failed": count("failed"),
        "error": count("error"),
    }


def _run_pytest(target: Path, python: str) -> dict:
    """Запускає один automated pytest-ціль; повертає доказ result/error."""
    evidence = {
        "target": str(target),
        "result": "FAIL",
        "error": "",
        "detail": {"passed": 0, "failed": 0, "error": 0},
    }
    if not target.is_file():
        evidence["result"] = "NOT_RUN"
        evidence["error"] = f"missing test target: {target.relative_to(ROOT)}"
        return evidence
    try:
        result = subprocess.run(
            [python, "-m", "pytest", "-q", str(target)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=1800,
        )
        evidence["detail"] = parse_pytest_summary(
            result.stdout + "\n" + result.stderr
        )
        evidence["result"] = "PASS" if result.returncode == 0 else "FAIL"
        if result.returncode != 0:
            evidence["error"] = (result.stderr or result.stdout).strip()[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
    return evidence


def proof_entry(
    scenario: dict,
    version: str,
    commit: str,
    env: dict,
    run: bool = False,
    python: str = sys.executable,
) -> dict:
    """Формує пункт доказу для одного сценарію.

    - run=False  → результат "NOT_RUN", всі automated цілі "NOT_RUN";
    - run=True і без automated цілей → результат "PROCEDURE" із посиланням на "rt";
    - run=True і з automated цілями → виконує pytest та збирає PASS/FAIL.
    """
    targets = scenario.get("automated", [])
    entry = {
        "id": scenario.get("id"),
        "name": scenario.get("name", scenario.get("id", "?")),
        "description": scenario.get("description", ""),
        "result": "NOT_RUN",
        "evidence": [],
    }

    if run and not targets:
        rt_target = scenario.get("rt")
        entry["evidence"] = [{"target": rt_target, "result": "PROCEDURE", "error": ""}]
        entry["result"] = "PROCEDURE"
    elif run:
        for target in targets:
            evidence = _run_pytest(_resolve(target), python)
            entry["evidence"].append(evidence)
        results = {item["result"] for item in entry["evidence"]}
        entry["result"] = "PASS" if results == {"PASS"} else "FAIL"
    else:
        entry["evidence"] = [
            {"target": str(_resolve(target)), "result": "NOT_RUN", "error": ""}
            for target in targets
        ]
        entry["result"] = "NOT_RUN"

    return entry


def build_report(
    scenarios: list[dict],
    version: str,
    commit: str,
    env: dict,
    run: bool = False,
    python: str = sys.executable,
) -> dict:
    """Збирає єдиний звіт кваліфікації в заданому форматі доказу."""
    entries = [proof_entry(scenario, version, commit, env, run=run, python=python)
               for scenario in scenarios]

    summary = {"total": len(entries), "passed": 0, "failed": 0, "not_run": 0, "procedure": 0}
    for entry in entries:
        result = (entry["result"] or "").upper()
        if result == "PASS":
            summary["passed"] += 1
        elif result == "FAIL":
            summary["failed"] += 1
        elif result == "PROCEDURE":
            summary["procedure"] += 1
        elif result == "NOT_RUN":
            summary["not_run"] += 1

    if run:
        overall = "PASS" if summary["failed"] == 0 and summary["total"] > 0 else "FAIL"
    else:
        overall = "PLAN"

    return {
        "tool": TOOL,
        "issue": ISSUE,
        "version": version,
        "commit": commit,
        "mode": "run" if run else "plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": env,
        "scenarios": entries,
        "summary": summary,
        "overall": overall,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Vertep automated infrastructure qualification (Issue %s)" % ISSUE
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Виконати автоматизовані pytest-цілі сценаріїв (інакше — план/NOT_RUN).",
    )
    parser.add_argument(
        "--selected", nargs="*", default=[],
        help="Лише вказані сценарії (S01, S03, ...). За замовчуванням — усі.",
    )
    parser.add_argument("--output", type=Path, help="Шлях для JSON-звіту.")
    parser.add_argument("--python", default=sys.executable, help="Інтерпретатор Python.")
    args = parser.parse_args(argv)

    version = load_version()
    commit = git_commit()
    env = environment()
    all_scenarios = collect_scenarios()
    scenarios = resolve_selected(all_scenarios, args.selected) if args.selected else all_scenarios
    report = build_report(scenarios, version, commit, env, run=args.run, python=args.python)

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["overall"] in ("PASS", "PLAN") else 1


if __name__ == "__main__":
    raise SystemExit(main())