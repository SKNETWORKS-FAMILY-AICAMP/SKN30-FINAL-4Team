#!/usr/bin/env python3
"""Validate and parse the five synthetic pre-review HWPX fixtures.

This is an offline regression harness.  It deliberately stops after
``HWPX -> Common IR -> CandidatePack/request-type preflight``: materialising a
Request Profile requires the configured LLM and is exercised separately by the
worker E2E test.  No OpenAI, database, Storage, or fixture writes occur here.

The generated HWPX files mimic the target request form; they are not evidence
that arbitrary Hancom-authored forms have identical layout behaviour.  The
assertions intentionally check the semantic text from their manifest rather
than parser-internal block IDs.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Iterable
from zipfile import BadZipFile, ZipFile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker.profiles import (  # noqa: E402
    StageError,
    build_pack,
    parse_to_common_ir,
    resolve_request_type,
)


DEFAULT_FIXTURE_DIR = (
    REPOSITORY_ROOT
    / "docs"
    / "pre_review_request_e2e_5_20260909_v1"
    / "generated"
)
EXPECTED_DATASET = "synthetic_prereview_request_e2e_5/v1"
EXPECTED_CASE_COUNT = 5
EXPECTED_REQUEST_TYPE = "detail_program_new"
EXPECTED_REQUEST_REASON_LABEL = "세부사업 신설"
REQUIRED_HWPX_MEMBERS = frozenset(
    {
        "mimetype",
        "version.xml",
        "Contents/content.hpf",
        "Contents/section0.xml",
        "META-INF/container.xml",
        "META-INF/manifest.xml",
    }
)
HWPX_MIMETYPE = b"application/hwp+zip"


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    filename: str
    source_notice_id: str
    program_name: str
    sha256: str
    expected_lines: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty_strings(values: Any, *, field: str, case_id: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{case_id}: {field} must be a list of strings")
    return [value for value in values if value.strip()]


def _load_cases(fixture_dir: Path) -> list[FixtureCase]:
    manifest_path = fixture_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read fixture manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("dataset") != EXPECTED_DATASET:
        raise ValueError(f"unexpected fixture dataset in {manifest_path}")
    if manifest.get("request_type") != EXPECTED_REQUEST_TYPE:
        raise ValueError(f"unexpected request type in {manifest_path}")
    if manifest.get("request_reason_label") != EXPECTED_REQUEST_REASON_LABEL:
        raise ValueError(f"unexpected request reason label in {manifest_path}")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"fixture manifest must contain exactly {EXPECTED_CASE_COUNT} cases")

    cases: list[FixtureCase] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("fixture manifest case must be an object")
        case_id = str(raw.get("case_id") or "")
        filename = str(raw.get("filename") or "")
        notice_id = str(raw.get("source_notice_id") or "")
        program_name = str(raw.get("program_name") or "")
        expected_hash = str(raw.get("sha256") or "").lower()
        if (
            not case_id
            or not filename.endswith(".hwpx")
            or Path(filename).name != filename
            or not notice_id
            or not program_name
            or raw.get("output") != filename
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise ValueError(f"invalid required fixture manifest value for {case_id or filename}")
        if case_id in seen_ids or filename in seen_names:
            raise ValueError(f"duplicate fixture case ID or filename: {case_id}, {filename}")
        seen_ids.add(case_id)
        seen_names.add(filename)
        expected_lines = tuple(
            [program_name]
            + _nonempty_strings(raw.get("need"), field="need", case_id=case_id)
            + _nonempty_strings(raw.get("main"), field="main", case_id=case_id)
            + _nonempty_strings(raw.get("effect"), field="effect", case_id=case_id)
        )
        cases.append(
            FixtureCase(
                case_id=case_id,
                filename=filename,
                source_notice_id=notice_id,
                program_name=program_name,
                sha256=expected_hash,
                expected_lines=expected_lines,
            )
        )
    manifest_names = {case.filename for case in cases}
    fixture_names = {path.name for path in fixture_dir.glob("*.hwpx") if path.is_file()}
    if fixture_names != manifest_names:
        missing = sorted(manifest_names - fixture_names)
        unexpected = sorted(fixture_names - manifest_names)
        raise ValueError(
            "fixture files do not match manifest: "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )
    return cases


def _validate_hwpx_container(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            members = set(archive.namelist())
            missing = sorted(REQUIRED_HWPX_MEMBERS - members)
            if missing:
                raise ValueError(f"{path.name}: required HWPX members missing: {', '.join(missing)}")
            if archive.read("mimetype") != HWPX_MIMETYPE:
                raise ValueError(f"{path.name}: unexpected HWPX mimetype")
    except (OSError, BadZipFile) as error:
        raise ValueError(f"{path.name}: unreadable HWPX container: {error}") from error


def _all_block_text(document: dict[str, Any]) -> str:
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("Common IR blocks must be a list")
    texts = [block.get("text") for block in blocks if isinstance(block, dict)]
    if not texts or not all(isinstance(text, str) for text in texts):
        raise ValueError("Common IR contains no text blocks")
    return "\n".join(texts)


def validate_fixture_case(case: FixtureCase, fixture_dir: Path, run_dir: Path) -> dict[str, Any]:
    """Verify one source and execute the production parser/preflight path."""

    source = fixture_dir / case.filename
    if not source.is_file():
        raise ValueError(f"{case.case_id}: fixture is missing: {source}")
    actual_hash = _sha256(source)
    if actual_hash != case.sha256:
        raise ValueError(
            f"{case.case_id}: SHA-256 mismatch: expected {case.sha256}, got {actual_hash}"
        )
    _validate_hwpx_container(source)

    try:
        artifact = parse_to_common_ir(
            input_path=source,
            notice_id=case.source_notice_id,
            source_kind="hwpx",
            run_dir=run_dir / case.case_id,
        )
    except StageError as error:
        raise ValueError(
            f"{case.case_id}: Common IR parse failed [{error.diagnostic.reason_code}]: "
            f"{error.diagnostic.message}"
        ) from error

    document = artifact.document
    provenance = document.get("document", {}).get("provenance", {})
    if provenance.get("source_sha256") != case.sha256:
        raise ValueError(f"{case.case_id}: Common IR provenance SHA-256 mismatch")
    all_text = _all_block_text(document)
    missing_lines = [line for line in case.expected_lines if line not in all_text]
    if missing_lines:
        raise ValueError(
            f"{case.case_id}: Common IR omitted manifest text: {missing_lines!r}"
        )

    # This is the deterministic Request Profile preflight.  It proves that the
    # parsed form has one valid server-owned request type before an LLM selector
    # is ever invoked.  Materialising facts is deliberately out of scope here.
    request_type = resolve_request_type(build_pack(document))
    if request_type.get("selected_code") != EXPECTED_REQUEST_TYPE:
        raise ValueError(
            f"{case.case_id}: expected {EXPECTED_REQUEST_TYPE}, "
            f"got {request_type.get('selected_code')!r}"
        )
    if request_type.get("value_raw") != EXPECTED_REQUEST_REASON_LABEL:
        raise ValueError(f"{case.case_id}: unexpected request type label")
    # The production parser must treat the fixture as immutable input.
    if _sha256(source) != actual_hash:
        raise ValueError(f"{case.case_id}: parser modified the source fixture")

    return {
        "case_id": case.case_id,
        "filename": case.filename,
        "source_notice_id": case.source_notice_id,
        "source_sha256": actual_hash,
        "common_ir_document_id": artifact.common_ir_document_id,
        "common_ir_block_count": artifact.block_count,
        "request_type": request_type["selected_code"],
    }


def _output_dir(path: Path | None, stack: ExitStack) -> Path:
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        return path
    # TemporaryDirectory is kept alive through all parser subprocesses.
    return Path(stack.enter_context(TemporaryDirectory(prefix="prereview-synthetic-hwpx-")))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help="directory containing manifest.json and five .hwpx sources",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="optional directory for Common IR artifacts (default: discarded temporary directory)",
    )
    args = parser.parse_args(argv)
    fixture_dir = args.fixture_dir.resolve()
    cases = _load_cases(fixture_dir)
    with ExitStack() as stack:
        run_dir = _output_dir(args.run_dir.resolve() if args.run_dir else None, stack)
        results = [validate_fixture_case(case, fixture_dir, run_dir) for case in cases]
    print(
        json.dumps(
            {
                "status": "ok",
                "dataset": EXPECTED_DATASET,
                "fixture_count": len(results),
                "cases": results,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print(f"fixture validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
