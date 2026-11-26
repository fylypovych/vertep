"""Deterministic document extraction, sanitization, prompt-injection scanning,
and optional OCR for Character Knowledge Library (#506 K2).

Runs exclusively on CPU Workers (never inside CORE, AGENTS.md §3.1).
Inspired by book-to-skill deterministic extraction without runtime dependency
on SKILL.md or external host CLIs.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Invisible / BiDi / control characters used in prompt-injection smuggling.
_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)

# Blocking prompt-injection patterns: untrusted sources are DATA, never instructions.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I)),
    ("override_system", re.compile(r"(override|disregard|forget)\s+(the\s+)?system\s+prompt", re.I)),
    ("role_hijack", re.compile(r"you\s+are\s+now\s+(a\s+)?(developer|admin|system|jailbroken)", re.I)),
    ("uk_ignore_instructions", re.compile(r"ігноруй\s+(усі\s+)?попередні\s+інструкції", re.I)),
    ("ru_ignore_instructions", re.compile(r"игнорируй\s+(все\s+)?предыдущие\s+инструкции", re.I)),
    ("prompt_tag_injection", re.compile(r"<\s*/?\s*(system|assistant|tool_call|instructions)\s*>", re.I)),
)

# Safety / dangerous instruction patterns flagged for review.
_SAFETY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dangerous_substance", re.compile(r"(гірк\w+\s+мигдал|горьк\w+\s+миндал|синильн\w+\s+кислот|cyanide)", re.I)),
    ("medical_claim", re.compile(r"(лікує\s+рак|лечит\s+рак|гарантовано\s+вилікує|cures\s+cancer)", re.I)),
)

MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024  # Billion-laughs / zip-bomb guard


def sanitize_text(raw: str) -> str:
    """Normalize Unicode and strip invisible/BiDi/control smuggling characters."""
    normalized = unicodedata.normalize("NFKC", raw or "")
    cleaned = _INVISIBLE_RE.sub("", normalized)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse excessive blank lines while preserving paragraph boundaries.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def scan_prompt_injection(text: str) -> list[str]:
    """Return list of matched prompt-injection rule identifiers (blocking before approval)."""
    reasons: list[str] = []
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text or ""):
            reasons.append(name)
    return reasons


def scan_safety_flags(text: str) -> list[str]:
    """Return list of matched content-safety warning flags for risk escalation."""
    flags: list[str] = []
    for name, pattern in _SAFETY_PATTERNS:
        if pattern.search(text or ""):
            flags.append(name)
    return flags


def _extract_docx_pages(data: bytes) -> list[dict[str, Any]]:
    """Safe DOCX extraction with zip-bomb and XXE/DOCTYPE entity guards."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError(f"Corrupted DOCX archive: {error}") from error
    with zf:
        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise ValueError("DOCX exceeds safe uncompressed size limit")
        if "word/document.xml" not in zf.namelist():
            raise ValueError("DOCX archive is missing word/document.xml")
        xml_bytes = zf.read("word/document.xml")
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise ValueError("DOCX XML contains forbidden DOCTYPE/ENTITY declarations (XXE guard)")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise ValueError(f"Invalid DOCX XML: {error}") from error
    paragraphs: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("}p"):
            texts = [child.text for child in elem.iter() if child.tag.endswith("}t") and child.text]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
    return [{"page_number": 1, "text": "\n\n".join(paragraphs), "ocr_used": False, "ocr_confidence": None}]


def _extract_html_pages(data: bytes) -> list[dict[str, Any]]:
    raw = data.decode("utf-8", errors="replace")
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    stripped = re.sub(r"<[^>]+>", "\n", no_scripts)
    text = html.unescape(stripped)
    return [{"page_number": 1, "text": text, "ocr_used": False, "ocr_confidence": None}]


def _extract_text_pages(data: bytes) -> list[dict[str, Any]]:
    raw = data.decode("utf-8", errors="replace")
    # Respect explicit form-feed page breaks when present.
    parts = raw.split("\f")
    return [
        {"page_number": idx, "text": part, "ocr_used": False, "ocr_confidence": None}
        for idx, part in enumerate(parts, 1)
    ]


def _run_tesseract_ocr(image_or_pdf: bytes, language: str = "ukr+eng") -> tuple[str, float]:
    """Run local tesseract/ocrmypdf CLI when installed; raises RuntimeError if missing."""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise RuntimeError("OCR requested but tesseract is not installed on this Worker")
    with tempfile.TemporaryDirectory(prefix="vertep-ocr-") as tmp:
        src = Path(tmp) / "input.bin"
        src.write_bytes(image_or_pdf)
        proc = subprocess.run(
            [tesseract, str(src), "stdout", "-l", language],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"OCR failed: {proc.stderr.strip()[:300]}")
        return proc.stdout, 0.85


def _extract_pdf_pages(
    data: bytes,
    *,
    enable_ocr: bool = False,
    ocr_language: str = "ukr+eng",
    ocr_runner: Callable[[bytes, str], tuple[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Fallback PDF extraction chain (pypdf -> pdftotext) + optional OCR for scanned pages."""
    if not data.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF signature")
    pages: list[dict[str, Any]] = []
    # 1. Try pypdf if available in environment.
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF is not supported")
        for idx, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            pages.append({"page_number": idx, "text": text, "ocr_used": False, "ocr_confidence": None})
    except ImportError:
        pages = []
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Corrupted PDF: {error}") from error

    # 2. Fallback to pdftotext subprocess if pypdf was not installed.
    if not pages and shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory(prefix="vertep-pdf-") as tmp:
            pdf_path = Path(tmp) / "doc.pdf"
            pdf_path.write_bytes(data)
            proc = subprocess.run(
                ["pdftotext", str(pdf_path), "-"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode == 0:
                for idx, chunk in enumerate(proc.stdout.split("\f"), 1):
                    if chunk.strip() or idx == 1:
                        pages.append({"page_number": idx, "text": chunk, "ocr_used": False, "ocr_confidence": None})

    # 3. Minimal deterministic fallback for simple text-stream PDFs when neither pypdf nor pdftotext is installed.
    if not pages:
        raw_latin = data.decode("latin-1", errors="ignore")
        # Extract literal strings inside PDF BT...ET blocks (simple uncompressed PDF streams).
        literals = re.findall(r"\(([^()]{2,})\)", raw_latin)
        text = "\n".join(literals)
        pages = [{"page_number": 1, "text": text, "ocr_used": False, "ocr_confidence": None}]

    # Optional OCR for image-only / scanned pages (no extracted text).
    if enable_ocr:
        runner = ocr_runner or _run_tesseract_ocr
        for page in pages:
            if len(sanitize_text(page["text"])) < 10:
                ocr_text, confidence = runner(data, ocr_language)
                page["text"] = ocr_text
                page["ocr_used"] = True
                page["ocr_confidence"] = float(confidence)
    return pages


def chunk_pages(
    pages: list[dict[str, Any]],
    *,
    max_chars: int = 800,
    overlap_chars: int = 80,
) -> list[dict[str, Any]]:
    """Split extracted pages into normalized chunks keeping exact page & section provenance."""
    chunks: list[dict[str, Any]] = []
    chunk_index = 0
    current_section: str | None = None

    for page in pages:
        page_number = int(page.get("page_number") or 1)
        cleaned = sanitize_text(str(page.get("text") or ""))
        if not cleaned:
            continue
        paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
        buffer = ""
        for para in paragraphs:
            heading = re.match(r"^#{1,4}\s+(.+)$", para)
            if heading:
                current_section = heading.group(1).strip()[:160]
            if buffer and len(buffer) + len(para) + 2 > max_chars:
                chunks.append(_build_chunk(buffer, page_number, current_section, chunk_index, page))
                chunk_index += 1
                tail = buffer[-overlap_chars:] if overlap_chars > 0 and len(buffer) > overlap_chars else ""
                buffer = (tail + "\n\n" + para).strip() if tail else para
            else:
                buffer = (buffer + "\n\n" + para).strip() if buffer else para
        if buffer:
            chunks.append(_build_chunk(buffer, page_number, current_section, chunk_index, page))
            chunk_index += 1
    return chunks


def _build_chunk(
    content: str,
    page_number: int,
    section_title: str | None,
    chunk_index: int,
    page: dict[str, Any],
) -> dict[str, Any]:
    injection_reasons = scan_prompt_injection(content)
    safety_flags = scan_safety_flags(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "chunk_index": chunk_index,
        "page_number": page_number,
        "section_title": section_title,
        "content": content,
        "content_sha256": digest,
        "ocr_used": bool(page.get("ocr_used", False)),
        "ocr_confidence": page.get("ocr_confidence"),
        "injection_flagged": bool(injection_reasons),
        "injection_reasons": injection_reasons,
        "safety_flags": safety_flags,
    }


def extract_document(
    *,
    data: bytes,
    source_type: str = "text",
    language: str = "uk",
    enable_ocr: bool = False,
    ocr_language: str | None = None,
    max_chars: int = 800,
    ocr_runner: Callable[[bytes, str], tuple[str, float]] | None = None,
) -> dict[str, Any]:
    """Deterministic document extraction returning page-mapped chunks, hashes and security flags."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Source document is empty")
    raw_bytes = bytes(data)
    if len(raw_bytes) > MAX_SOURCE_BYTES:
        raise ValueError("Source document exceeds size limit")

    kind = (source_type or "text").strip().lower()
    ocr_lang = ocr_language or ("ukr+eng" if language == "uk" else f"{language}+eng")

    if kind in {"text", "txt", "markdown", "md"}:
        pages = _extract_text_pages(raw_bytes)
    elif kind in {"html", "htm"}:
        pages = _extract_html_pages(raw_bytes)
    elif kind == "docx":
        pages = _extract_docx_pages(raw_bytes)
    elif kind == "pdf":
        pages = _extract_pdf_pages(raw_bytes, enable_ocr=enable_ocr, ocr_language=ocr_lang, ocr_runner=ocr_runner)
    elif kind in {"image", "scan"}:
        if not enable_ocr:
            raise ValueError("Image/scan source requires enable_ocr=True")
        runner = ocr_runner or _run_tesseract_ocr
        text, conf = runner(raw_bytes, ocr_lang)
        pages = [{"page_number": 1, "text": text, "ocr_used": True, "ocr_confidence": float(conf)}]
    else:
        raise ValueError(f"Unsupported source_type: {source_type}")

    chunks = chunk_pages(pages, max_chars=max_chars)
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return {
        "source_sha256": source_sha256,
        "source_type": kind,
        "language": language,
        "page_count": len(pages),
        "ocr_used": any(p.get("ocr_used") for p in pages),
        "injection_detected": any(c["injection_flagged"] for c in chunks),
        "safety_flags": sorted({flag for c in chunks for flag in c["safety_flags"]}),
        "chunks": chunks,
    }


def extract_from_task(task: dict[str, Any], *, ocr_runner=None) -> dict[str, Any]:
    """Worker task entry: accepts ``data_base64`` or ``text`` and returns structured extraction."""
    if task.get("data_base64"):
        try:
            raw = base64.b64decode(task["data_base64"])
        except Exception as error:
            raise ValueError(f"Invalid data_base64 payload: {error}") from error
    elif isinstance(task.get("text"), str):
        raw = task["text"].encode("utf-8")
    else:
        raise ValueError("knowledge_extract task requires data_base64 or text")

    return extract_document(
        data=raw,
        source_type=str(task.get("source_type") or "text"),
        language=str(task.get("language") or "uk"),
        enable_ocr=bool(task.get("enable_ocr", False)),
        ocr_language=task.get("ocr_language"),
        max_chars=int(task.get("max_chars") or 800),
        ocr_runner=ocr_runner,
    )
