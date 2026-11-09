"""Compare per-test outcomes of JUnit XML reports (#439): exit 1 on any difference.

Usage: compare_junit.py REFERENCE.xml OTHER.xml [OTHER.xml ...]
Each OTHER report is compared with REFERENCE. Every report's totals and its raw
whole-module skip records are printed too, so nothing is hidden by normalisation.
"""
import re
import sys
import xml.etree.ElementTree as ET


def _cases(path):
    return list(ET.parse(path).iter("testcase"))


def _outcome(case):
    tags = {child.tag for child in case}
    return next((t for t in ("failure", "error", "skipped") if t in tags), "passed")


def module_skips(path):
    """Whole-module skips: pytest reports them without a classname, only when run without xdist."""
    return sorted(case.get("name") for case in _cases(path)
                  if not case.get("classname") and _outcome(case) == "skipped")


def outcomes(path):
    result = {}
    for case in _cases(path):
        if not case.get("classname") and _outcome(case) == "skipped":
            continue  # listed separately by module_skips()
        # xdist --dist loadgroup appends "@<group>" to grouped test names.
        name = re.sub(r"@\w+$", "", f"{case.get('classname')}::{case.get('name')}")
        result[name] = _outcome(case)
    return result


def summary(path):
    counts = {"passed": 0, "failure": 0, "error": 0, "skipped": 0}
    for value in outcomes(path).values():
        counts[value] += 1
    seconds = sum(float(suite.get("time") or 0) for suite in ET.parse(path).iter("testsuite"))
    return {**counts, "cases": sum(counts.values()), "suite_time_s": round(seconds, 1)}


def diff(a, b):
    return sorted(f"{n}: {a.get(n, 'missing')} != {b.get(n, 'missing')}" for n in a.keys() | b.keys() if a.get(n) != b.get(n))


def main(paths):
    for path in paths:
        print(f"{path}: {summary(path)}")
        for name in module_skips(path):
            print(f"  whole-module skip (raw record, not counted as a case): {name}")
    reference, failed = outcomes(paths[0]), False
    for other in paths[1:]:
        lines = diff(reference, outcomes(other))
        print(f"{paths[0]} vs {other}: " + ("Outcomes identical" if not lines else f"{len(lines)} differences"))
        print("\n".join(lines)) if lines else None
        failed |= bool(lines)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
