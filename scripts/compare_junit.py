"""Compare per-test outcomes of two JUnit XML reports (#439): exit 1 on any difference."""
import sys
import xml.etree.ElementTree as ET


def outcomes(path):
    result = {}
    for case in ET.parse(path).iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        tags = {child.tag for child in case}
        result[name] = next((t for t in ("failure", "error", "skipped") if t in tags), "passed")
    return result


def diff(a, b):
    return sorted(f"{n}: {a.get(n, 'missing')} != {b.get(n, 'missing')}" for n in a.keys() | b.keys() if a.get(n) != b.get(n))


if __name__ == "__main__":
    lines = diff(outcomes(sys.argv[1]), outcomes(sys.argv[2]))
    print("\n".join(lines) or "Outcomes identical")
    sys.exit(1 if lines else 0)
