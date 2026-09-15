"""Shared helpers for the Common IR v1 adapters (PDF and HWP/HWPX).

v1's goals over v0.1/v0.3 (see common_ir_v1_schema.py for the schema itself):
  - one output per source file; no paired-source dependency.
  - document.provenance carries source_sha256/generator/generator_version/
    schema_version so an output is self-describing and reproducible, plus
    parser/parser_version/parser_core_version (see make_document) so the
    *upstream* tool that produced the raw IR (rhwp, pdf_inspector) stays
    traceable independently of this adapter's own generator/generator_version.
  - blocks are semantic units (merged paragraphs / rhwp paragraphs / table
    rows), not one block per native text fragment.
  - relations may be inferred=true (v0.1+ forbids this outright); nothing is
    silently coerced to false or dropped.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

GENERATOR_NAME = "common_ir_v1_adapters"
GENERATOR_VERSION = "1.1.0"
SCHEMA_VERSION = "common_ir_v1"

# Source-derived structural boundary markers this project cares about
# ("[서식 N]" order forms, "【붙임 N】" attachments, "[별첨 N]" annexes).
# Detection only -- the marker is recorded as metadata on whichever block's
# text contains it; it never changes structure_status or invents a split.
_BOUNDARY_PATTERNS = [
    ("서식", re.compile(r"\[\s*서식\s*[0-9]*\s*\]")),
    ("붙임", re.compile(r"【\s*붙임\s*[0-9]*\s*】")),
    ("별첨", re.compile(r"\[\s*별첨\s*[0-9]*\s*\]")),
]


def detect_boundary_markers(text: str) -> list[dict]:
    text = text or ""
    found = []
    for label, pattern in _BOUNDARY_PATTERNS:
        for match in pattern.finditer(text):
            found.append({"marker": label, "matched_text": match.group(0)})
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_provenance(method: str, page: int | None, bbox, coordinate_space: str | None, source_location: str, **extra) -> dict:
    provenance = {"method": method, "page": page, "bbox": bbox, "coordinate_space": coordinate_space, "source_location": source_location}
    provenance.update({k: v for k, v in extra.items() if v is not None})
    return provenance


def make_document(
    document_id: str, source_kind: str, source_path: Path, page_count: int | None, raw_artifact_ids: list[str], method: str,
    *, source_sha256: str | None = None, parser: str | None = None, parser_version: str | None = None,
    parser_core_version: str | None = None,
) -> dict:
    """`parser`/`parser_version` name the *upstream* tool that produced the
    raw IR this document is adapted from (e.g. "rhwp"/rhwp.version(), or
    "pdf_inspector"/the pdf_inspector raw capture's own "version") --
    independent of generator/generator_version below, which always name
    this adapter script itself. `parser_core_version` is rhwp's own
    native/core library version (rhwp.rhwp_core_version()); pass it only
    when the caller actually has it (e.g. never for PDF) -- make_provenance
    drops None extras, so an unavailable value is simply absent from the
    output rather than recorded as a guess."""
    source_path = Path(source_path)
    if source_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 hex digest")
        if source_path.is_file() and sha256_file(source_path) != source_sha256:
            raise ValueError("source_sha256 does not match source_path")
    elif not source_path.is_file():
        raise ValueError("source_path must exist when source_sha256 is not supplied")

    return {
        "document_id": document_id,
        "source_kind": source_kind,
        "artifact_role": "production",
        "page_count": page_count,
        "raw_artifact_ids": raw_artifact_ids,
        "provenance": make_provenance(
            method=method, page=None, bbox=None, coordinate_space=None, source_location=str(source_path),
            source_sha256=source_sha256 or sha256_file(source_path), generator=GENERATOR_NAME,
            generator_version=GENERATOR_VERSION, schema_version=SCHEMA_VERSION,
            parser=parser, parser_version=parser_version, parser_core_version=parser_core_version,
        ),
    }


def new_document_shell(
    document_id: str, source_kind: str, source_path: Path, page_count: int | None, raw_artifact_ids: list[str], method: str,
    *, source_sha256: str | None = None, parser: str | None = None, parser_version: str | None = None,
    parser_core_version: str | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "document": make_document(
            document_id, source_kind, source_path, page_count, raw_artifact_ids, method,
            source_sha256=source_sha256, parser=parser, parser_version=parser_version,
            parser_core_version=parser_core_version,
        ),
        "blocks": [],
        "conflicts": [],
        "relations": [],
    }


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
