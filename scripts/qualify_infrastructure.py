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

Режими та коди виходу (Issue i.0.0.0.89):
- `--run` без помилок: overall="PASS" лише коли ВСІ сценарії мають PASS
  (passed>0, failed==0, skipped==0 для кожної pytest-цілі); будь-який
  all-skipped/zero-tests прогін дає NO_TESTS і не вважається доказом;
- `--run` з PROCEDURE-пунктами: overall="PARTIAL" (код 1), доказом стає
  PASS лише з явним прапорцем `--allow-procedures`;
- без `--run`: overall="PLAN" (статичний каталог, не доказ) → код 1;
  код 0 у PLAN-режимі дає лише явний `--plan-only`;
- код 0 повертається тільки для overall="PASS".
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

VALID_RT_ISSUES = {34, 35, 42, 47, 54, 63}

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
        "rt_issue": 35,
        "description": "Automated/disposable qualification чистого Ubuntu-сумісного "
                       "bootstrap: roles, секрети, контейнери, health check.",
    },
    {
        "id": "S02",
        "name": "CORE + Worker enrollment, certificates, self-test, task/result, remote-control",
        "automated": ["tests/test_node_registry.py", "tests/test_fleet_controls.py", "tests/test_worker_self_test.py"],
        "rt": "rt::S02 фізичний multi-host enrollment + certificates",
        "rt_issue": 42,
        "description": "Реєстрація вузлів, сертифікати, self-test, task/result "
                       "та remote-control сценарії між CORE та Worker.",
    },
    {
        "id": "S03",
        "name": "GPU/ComfyUI-compatible integration harness",
        "automated": ["tests/test_core_generation_gate.py", "tests/test_compute_distributed.py"],
        "rt": "rt::S03 фізична GPU + ComfyUI workflow",
        "rt_issue": 34,
        "description": "Інтеграційний harness сумісний з GPU/ComfyUI без вимоги "
                       "фізичної GPU: генерація через mock/fallback, execution op.",
    },
    {
        "id": "S04",
        "name": "Backup → data mutation → restore → health verification",
        "automated": ["tests/test_role_services.py"],
        "rt": "rt::S04 реальний backup/restore стенд",
        "rt_issue": 35,
        "description": "Резервна копія, мутація даних, відновлення та перевірка "
                       "здоров'я у контрольованому середовищі.",
    },
    {
        "id": "S05",
        "name": "Existing-install migration/persistence harness",
        "automated": ["tests/test_persistent_user_data.py", "tests/test_deployment_plan.py"],
        "rt": "rt::S05 міграція існуючої інсталяції",
        "rt_issue": 35,
        "description": "characters/brands/workflows/jobs → update/recreate/"
                       "rollback/restore simulation без втрати даних.",
    },
    {
        "id": "S06",
        "name": "Safe Update fault-injection",
        "automated": ["tests/test_safe_update.py", "tests/test_rolling_update.py", "tests/test_updates.py"],
        "rt": "rt::S06 real update interruption/restart/rollback",
        "rt_issue": 35,
        "description": "rolling/canary, interruption/restart, rollback/recovery "
                       "з інжекцією відмов під час оновлення.",
    },
    {
        "id": "S07",
        "name": "Release trust automated checks",
        "automated": ["tests/test_key_lifecycle.py", "tests/test_update_security.py", "tests/test_release_qualification.py"],
        "rt": "rt::S07 key ceremony + recovery",
        "rt_issue": 54,
        "description": "signed artifacts, tamper/revoke/downgrade, key rotation "
                       "та recovery procedure.",
    },
    {
        "id": "S08",
        "name": "Publisher sandbox/fake integration з перевірюваним receipt",
        "automated": ["tests/test_publisher_live_adapters.py", "tests/test_publish_task_helpers.py"],
        "rt": "rt::S08 live platform publish receipt",
        "rt_issue": 47,
        "description": "Sandbox/fake publisher із перевірюваним receipt публікації.",
    },
]


def validate_rt_issues(scenarios: list[dict] | None = None) -> list[str]:
    """Перевіряє, що кожен rt_issue належить до чинного відкритого ir-набору.

    Набір відповідає фізичним/live сценаріям i.0.0.0.89:
    #34 (медіаконвеєр), #35 (встановлення/міграція/backup/update),
    #42 (node roles/web/telegram), #47 (publisher), #54 (security/release
    trust), #63 (Real Test Runner).  Повертає список порушень; порожній
    список означає, що каталог валідний.
    """
    items = SCENARIOS if scenarios is None else scenarios
    problems: list[str] = []
    for scenario in items:
        issue = scenario.get("rt_issue")
        if issue not in VALID_RT_ISSUES:
            problems.append(
                "%s: rt_issue=%r is outside the valid ir set %s"
                % (scenario.get("id"), issue, sorted(VALID_RT_ISSUES))
            )
    return problems


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


def find_scenario_by_rt_id(rt_id: str) -> dict | None:
    """Повертає сценарій за реальним test ідентифікатором (rt::S0X)."""
    for scenario in SCENARIOS:
        if scenario.get("rt", "").split()[0] == rt_id:
            return dict(scenario)
    return None


def find_scenario_by_issue(rt_issue_number: int) -> dict | None:
    """Повертає сценарій, прив'язаний до конкретного GitHub ir Issue."""
    for scenario in SCENARIOS:
        if scenario.get("rt_issue") == rt_issue_number:
            return dict(scenario)
    return None


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
    """Витягує лічильники passed/failed/error/skipped з короткого звіту pytest -q."""
    def count(needle: str) -> int:
        match = re.search(r"(\d+)\s+" + re.escape(needle), output)
        return int(match.group(1)) if match else 0

    return {
        "passed": count("passed"),
        "failed": count("failed"),
        "error": count("error"),
        "skipped": count("skipped"),
    }


def _run_pytest(target: Path, python: str) -> dict:
    """Запускає один automated pytest-ціль; повертає доказ result/error.

    PASS — лише якщо щонайменше один тест пройшов і жоден не пропущений
    чи не впав: "0 passed, N skipped" та порожні прогони дають NO_TESTS,
    а наявність skip робить прогін FAIL (skipped ≠ passed, AGENTS.md §21).
    """
    evidence = {
        "target": str(target),
        "result": "FAIL",
        "error": "",
        "detail": {"passed": 0, "failed": 0, "error": 0, "skipped": 0},
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
        detail = parse_pytest_summary(result.stdout + "\n" + result.stderr)
        evidence["detail"] = detail
        tail = (result.stderr or result.stdout).strip()[-2000:]
        if (
            result.returncode == 0
            and detail["failed"] == 0
            and detail["error"] == 0
            and detail["passed"] > 0
            and detail["skipped"] == 0
        ):
            evidence["result"] = "PASS"
        elif (
            detail["failed"] == 0
            and detail["error"] == 0
            and detail["passed"] == 0
            and result.returncode in (0, 5)
        ):
            evidence["result"] = "NO_TESTS"
            evidence["error"] = (
                f"no tests executed: passed=0, skipped={detail['skipped']} "
                "(all-skipped or empty run is not qualification proof)"
            )
        else:
            evidence["result"] = "FAIL"
            evidence["error"] = (
                f"pytest summary: passed={detail['passed']} failed={detail['failed']} "
                f"error={detail['error']} skipped={detail['skipped']} "
                f"returncode={result.returncode}\n{tail}"
            ).strip()
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
    - run=True і з automated цілями → виконує pytest: PASS лише коли всі
      цілі PASS; наявність хоча б однієї NO_TESTS/FAIL/NOT_RUN — не PASS.
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
        if results == {"PASS"}:
            entry["result"] = "PASS"
        elif results == {"NO_TESTS"}:
            entry["result"] = "NO_TESTS"
        else:
            entry["result"] = "FAIL"
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
    allow_procedures: bool = False,
) -> dict:
    """Збирає єдиний звіт кваліфікації в заданому форматі доказу.

    У режимі `run` overall="PASS" лише коли кожен пункт має PASS.
    PROCEDURE/NOT_RUN пункти ніколи не дають PASS: без
    `allow_procedures` результат — "PARTIAL", з ним — "PASS" лише за
    умови відсутності FAIL/NO_TESTS.  Порожній вибір — FAIL.
    """
    entries = [proof_entry(scenario, version, commit, env, run=run, python=python)
               for scenario in scenarios]

    summary = {"total": len(entries), "passed": 0, "failed": 0, "not_run": 0,
               "procedure": 0, "no_tests": 0}
    for entry in entries:
        result = (entry["result"] or "").upper()
        if result == "PASS":
            summary["passed"] += 1
        elif result == "FAIL":
            summary["failed"] += 1
        elif result == "PROCEDURE":
            summary["procedure"] += 1
        elif result == "NO_TESTS":
            summary["no_tests"] += 1
        elif result == "NOT_RUN":
            summary["not_run"] += 1

    if run:
        if summary["total"] == 0 or summary["failed"] or summary["no_tests"]:
            overall = "FAIL"
        elif summary["passed"] == summary["total"]:
            overall = "PASS"
        elif (
            allow_procedures
            and summary["passed"] + summary["procedure"] == summary["total"]
        ):
            overall = "PASS"
        elif summary["procedure"] or summary["not_run"]:
            overall = "PARTIAL"
        else:
            overall = "FAIL"
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


def _ensure_utf8_stdout() -> None:
    """Гарантує UTF-8 виведення JSON на Windows-консолях (cp1251 тощо)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
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
    parser.add_argument(
        "--allow-procedures", action="store_true",
        help="У режимі --run вважати наявність PROCEDURE-пунктів прийнятною "
             "частковою кваліфікацією (без прапорця такий прогін — PARTIAL, код 1).",
    )
    parser.add_argument(
        "--plan-only", action="store_true",
        help="Дозволити код 0 для статичного PLAN-каталогу (не є доказом).",
    )
    args = parser.parse_args(argv)

    problems = validate_rt_issues()
    if problems:
        for problem in problems:
            print(f"rt_issue validation: {problem}", file=sys.stderr)
        return 1

    version = load_version()
    commit = git_commit()
    env = environment()
    all_scenarios = collect_scenarios()
    scenarios = resolve_selected(all_scenarios, args.selected) if args.selected else all_scenarios
    if args.selected and not scenarios:
        print(f"unknown scenario ids: {', '.join(args.selected)}", file=sys.stderr)
        return 1
    report = build_report(
        scenarios, version, commit, env, run=args.run, python=args.python,
        allow_procedures=args.allow_procedures,
    )

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if report["overall"] == "PASS":
        return 0
    if report["overall"] == "PLAN" and args.plan_only:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())