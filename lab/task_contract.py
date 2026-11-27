"""Fail-closed Task Contract for laboratory coding tasks.

Before starting any new code-change task in LAB, the agent must fill out four
mandatory fields defined in ``lab/task_contract.md``:

1. ``files_to_change`` — Що змінюємо (файли)
2. ``invariants`` — Які інваріанти не можна зламати
3. ``no_gpu_verification`` — Як перевіряємо без GPU
4. ``proof_artifact`` — Який артефакт буде доказом

Only after all four fields pass validation may coding / role execution start.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

TASK_CONTRACT_FORMAT = "lab_task_contract/v1"
TEMPLATE_PATH = Path(__file__).with_name("task_contract.md")

#: Task kinds that do not modify repository code and therefore do not require a
#: code-change Task Contract (if one is supplied, it is still validated).
NON_CODE_TASK_KINDS = frozenset({"audit", "review", "report", "test", "verify"})

REQUIRED_FIELDS: tuple[str, ...] = (
    "files_to_change",
    "invariants",
    "no_gpu_verification",
    "proof_artifact",
)

FIELD_LABELS_UK: dict[str, str] = {
    "files_to_change": "Що змінюємо (файли)",
    "invariants": "Які інваріанти не можна зламати",
    "no_gpu_verification": "Як перевіряємо без GPU",
    "proof_artifact": "Який артефакт буде доказом",
}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "files_to_change": ("files_to_change", "files", "що змінюємо", "що змінюємо (файли)"),
    "invariants": ("invariants", "які інваріанти не можна зламати", "інваріанти"),
    "no_gpu_verification": (
        "no_gpu_verification",
        "verification_no_gpu",
        "як перевіряємо без gpu",
    ),
    "proof_artifact": (
        "proof_artifact",
        "evidence_artifact",
        "який артефакт буде доказом",
        "артефакт",
    ),
}

_HEADING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("files_to_change", re.compile(r"^#{1,6}\s*(?:1[.)]\s*)?що\s+змінюємо", re.IGNORECASE)),
    ("invariants", re.compile(r"^#{1,6}\s*(?:2[.)]\s*)?які\s+інваріанти", re.IGNORECASE)),
    ("no_gpu_verification", re.compile(r"^#{1,6}\s*(?:3[.)]\s*)?як\s+перевіряємо\s+без\s+gpu", re.IGNORECASE)),
    ("proof_artifact", re.compile(r"^#{1,6}\s*(?:4[.)]\s*)?який\s+артефакт\s+буде\s+доказом", re.IGNORECASE)),
)

_PLACEHOLDERS = frozenset({
    "", "-", "--", "---", "...", "…", "?", "???", "tbd", "todo", "fixme",
    "n/a", "none", "null", "placeholder", "заповнити", "буде пізніше",
})
_ANGLE_PLACEHOLDER_RE = re.compile(r"^<[^<>]+>$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class TaskContractError(ValueError):
    """Raised when a laboratory Task Contract is missing or incomplete."""


@dataclass(frozen=True)
class TaskContract:
    """Validated 4-point contract required before any LAB coding task starts."""

    files_to_change: tuple[str, ...]
    invariants: tuple[str, ...]
    no_gpu_verification: tuple[str, ...]
    proof_artifact: str

    @property
    def digest(self) -> str:
        body = {
            "format": TASK_CONTRACT_FORMAT,
            "files_to_change": list(self.files_to_change),
            "invariants": list(self.invariants),
            "no_gpu_verification": list(self.no_gpu_verification),
            "proof_artifact": self.proof_artifact,
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def as_dict(self) -> dict:
        return {
            "format": TASK_CONTRACT_FORMAT,
            "files_to_change": list(self.files_to_change),
            "invariants": list(self.invariants),
            "no_gpu_verification": list(self.no_gpu_verification),
            "proof_artifact": self.proof_artifact,
            "sha256": self.digest,
        }


def requires_task_contract(kind: str) -> bool:
    """True when ``kind`` is a code-change task requiring a filled Task Contract."""
    return str(kind or "").strip().lower() not in NON_CODE_TASK_KINDS


def load_task_contract_template(path: Path | str | None = None) -> str:
    """Read the canonical Task Contract template from the repository."""
    source = Path(path) if path is not None else TEMPLATE_PATH
    return source.read_text(encoding="utf-8")


def _clean_item(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("- ") or text.startswith("* "):
        text = text[2:].strip()
    elif re.match(r"^\d+[.)]\s+", text):
        text = re.sub(r"^\d+[.)]\s+", "", text).strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    return text


def _is_placeholder(value: str) -> bool:
    cleaned = _clean_item(value)
    lowered = cleaned.lower()
    if lowered in _PLACEHOLDERS:
        return True
    if _ANGLE_PLACEHOLDER_RE.fullmatch(cleaned):
        return True
    if any(token in lowered for token in ("<path/", "<інваріант", "<команда", "<шлях")):
        return True
    return False


def _validate_relative_path(raw: str) -> str:
    cleaned = _clean_item(raw)
    if _is_placeholder(cleaned):
        raise TaskContractError("files_to_change contains an unfilled placeholder")
    if cleaned.startswith("/") or cleaned.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", cleaned):
        raise TaskContractError(f"files_to_change must use repository-relative paths, got {cleaned!r}")
    parts = Path(cleaned.replace("\\", "/")).parts
    if not parts or ".." in parts:
        raise TaskContractError(f"files_to_change contains an unsafe path: {cleaned!r}")
    if cleaned in {".", "*", "**", "./"}:
        raise TaskContractError(f"files_to_change must name concrete files, got {cleaned!r}")
    return cleaned.replace("\\", "/")


def _extract_list(value, field_name: str) -> list[str]:
    if isinstance(value, str):
        items = [_clean_item(line) for line in value.splitlines() if line.strip()]
    elif isinstance(value, (list, tuple)):
        items = [_clean_item(str(item)) for item in value if str(item or "").strip()]
    else:
        return []
    result = [item for item in items if item and not _is_placeholder(item)]
    return result


def parse_markdown_contract(markdown: str) -> dict:
    """Parse a filled ``lab/task_contract.md`` document into the 4 contract fields."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise TaskContractError("Task Contract markdown is empty")
    stripped = _HTML_COMMENT_RE.sub("", markdown)
    buckets: dict[str, list[str]] = {field: [] for field in REQUIRED_FIELDS}
    current: str | None = None
    in_fence = False

    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched_field = None
        if line.startswith("#"):
            for field_name, pattern in _HEADING_PATTERNS:
                if pattern.match(line):
                    matched_field = field_name
                    break
            current = matched_field
            continue
        if current is None or not line or line.startswith(">") or line == "---":
            continue
        buckets[current].append(_clean_item(line))

    return {
        "files_to_change": [item for item in buckets["files_to_change"] if item],
        "invariants": [item for item in buckets["invariants"] if item],
        "no_gpu_verification": [item for item in buckets["no_gpu_verification"] if item],
        "proof_artifact": "; ".join(item for item in buckets["proof_artifact"] if item),
    }


def _resolve_field(mapping: dict, canonical: str):
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for alias in _FIELD_ALIASES[canonical]:
        if alias in lowered:
            return lowered[alias]
    return None


def validate_task_contract(raw: dict | str | TaskContract | None) -> TaskContract:
    """Validate all 4 mandatory Task Contract items fail-closed."""
    if isinstance(raw, TaskContract):
        return raw
    if raw is None:
        raise TaskContractError(
            "Task Contract is required before coding: fill all 4 sections "
            "(Що змінюємо (файли), Які інваріанти не можна зламати, "
            "Як перевіряємо без GPU, Який артефакт буде доказом)"
        )
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            raise TaskContractError("Task Contract is empty")
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except ValueError as error:
                raise TaskContractError(f"Invalid Task Contract JSON: {error}") from error
        else:
            data = parse_markdown_contract(stripped)
    elif isinstance(raw, dict):
        data = raw
    else:
        raise TaskContractError("Task Contract must be an object or markdown string")

    if not isinstance(data, dict):
        raise TaskContractError("Task Contract must resolve to an object")

    problems: list[str] = []

    raw_files = _resolve_field(data, "files_to_change")
    files_items = _extract_list(raw_files, "files_to_change")
    validated_files: list[str] = []
    if not files_items:
        problems.append(f"missing {FIELD_LABELS_UK['files_to_change']} (files_to_change)")
    else:
        for entry in files_items:
            try:
                validated_files.append(_validate_relative_path(entry))
            except TaskContractError as error:
                problems.append(str(error))

    invariants = _extract_list(_resolve_field(data, "invariants"), "invariants")
    if not invariants:
        problems.append(f"missing {FIELD_LABELS_UK['invariants']} (invariants)")

    no_gpu = _extract_list(_resolve_field(data, "no_gpu_verification"), "no_gpu_verification")
    if not no_gpu:
        problems.append(f"missing {FIELD_LABELS_UK['no_gpu_verification']} (no_gpu_verification)")

    raw_proof = _resolve_field(data, "proof_artifact")
    if isinstance(raw_proof, (list, tuple)):
        proof_parts = [item for item in (_clean_item(str(x)) for x in raw_proof) if item and not _is_placeholder(item)]
        proof_artifact = "; ".join(proof_parts)
    elif isinstance(raw_proof, str):
        proof_parts = [item for item in (_clean_item(line) for line in raw_proof.splitlines()) if item and not _is_placeholder(item)]
        proof_artifact = "; ".join(proof_parts)
    else:
        proof_artifact = ""
    if not proof_artifact:
        problems.append(f"missing {FIELD_LABELS_UK['proof_artifact']} (proof_artifact)")

    if problems:
        raise TaskContractError("Invalid Task Contract: " + "; ".join(problems))

    return TaskContract(
        files_to_change=tuple(dict.fromkeys(validated_files)),
        invariants=tuple(invariants),
        no_gpu_verification=tuple(no_gpu),
        proof_artifact=proof_artifact,
    )


def load_task_contract_file(path: Path | str) -> TaskContract:
    """Load and validate a Task Contract from a ``.md`` or ``.json`` file."""
    source = Path(path)
    if not source.is_file():
        raise TaskContractError(f"Task Contract file not found: {source}")
    return validate_task_contract(source.read_text(encoding="utf-8"))
