"""Focused contracts for the pinned pristine hard-6 model-input loader."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from worker.evaluation import pristine_common_ir as loader


IDS = tuple(f"PBLN_{value:015d}" for value in range(1, 7))


def _document(notice_id: str, source_format: str, source_sha256: str) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"{source_format}:{notice_id}",
            "source_kind": source_format,
            "artifact_role": "production",
            "provenance": {
                "source_sha256": source_sha256,
                "source_location": f"source.{source_format}",
                "method": "pdf_native_only" if source_format == "pdf" else "rhwp",
                "generator": "common_ir_v1_adapters",
                "generator_version": "1.1.0",
                "parser": "pdf_inspector" if source_format == "pdf" else "rhwp",
                "parser_version": "1.17.0" if source_format == "pdf" else "0.8.1",
                "page": None,
                "bbox": None,
                "coordinate_space": None,
                "schema_version": "common_ir_v1",
            },
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }
    if source_format == "pdf":
        result["document"].update(  # type: ignore[union-attr]
            {
                "page_count": 1,
                "native_text_page_count": 1,
                "pdf_semantic_eligibility": "eligible_native_text",
                "pdf_semantic_reason": "substantive_native_text_available",
            }
        )
    return result


def _info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.external_attr = 0o100600 << 16
    info.create_system = 3
    return info


def _fixture(tmp_path: Path, *, contaminated: bool = False) -> tuple[Path, Path]:
    archive_path = tmp_path / "input.zip"
    notices: list[dict[str, str]] = []
    with ZipFile(archive_path, "w", compression=ZIP_STORED) as archive:
        for index, notice_id in enumerate(IDS):
            source_format = "hwp" if index == 2 else "pdf"
            source_hash = sha256(f"source:{notice_id}".encode()).hexdigest()
            document = _document(notice_id, source_format, source_hash)
            if contaminated and index == 0:
                document["document"]["provenance"]["generator"] = "manual_gold_adjudication"  # type: ignore[index]
            raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
            member = f"{notice_id}/pipeline/common_ir_v1/{notice_id}.{source_format}.json"
            archive.writestr(_info(member), raw)
            notices.append({
                "notice_id": notice_id,
                "format": source_format,
                "archive_member": member,
                "source_sha256": source_hash,
                "common_ir_sha256": sha256(raw).hexdigest(),
            })
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": loader.PRISTINE_HARD6_SCHEMA_VERSION,
        "notice_count": 6,
        "notices": notices,
        "archive_sha256": sha256(archive_path.read_bytes()).hexdigest(),
    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return archive_path, manifest_path


def _pin_fixture(monkeypatch: pytest.MonkeyPatch, archive_path: Path, manifest_path: Path) -> None:
    monkeypatch.setattr(loader, "PRISTINE_HARD6_ARCHIVE_SHA256", sha256(archive_path.read_bytes()).hexdigest())
    monkeypatch.setattr(loader, "PRISTINE_HARD6_MANIFEST_PATH", manifest_path)


def test_loads_exact_six_member_input_with_manifest_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, manifest = _fixture(tmp_path)
    _pin_fixture(monkeypatch, archive, manifest)

    loaded = loader.load_pristine_common_ir(archive, manifest_path=manifest)

    assert tuple(loaded.documents) == IDS
    assert loaded.archive_sha256 == sha256(archive.read_bytes()).hexdigest()
    assert set(loaded.member_sha256) == set(IDS)


def test_rejects_manual_metadata_even_when_member_and_archive_pins_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, manifest = _fixture(tmp_path, contaminated=True)
    _pin_fixture(monkeypatch, archive, manifest)

    with pytest.raises(loader.PristineCommonIrError, match="manual adjudication provenance"):
        loader.load_pristine_common_ir(archive, manifest_path=manifest)


def test_external_manifest_cannot_replace_checked_in_trust_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, manifest = _fixture(tmp_path)
    _pin_fixture(monkeypatch, archive, manifest)
    external = tmp_path / "external.json"
    changed = json.loads(manifest.read_text(encoding="utf-8"))
    changed["notices"][0]["source_sha256"] = "0" * 64
    external.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(loader.PristineCommonIrError, match="does not match checked-in pin"):
        loader.load_pristine_common_ir(archive, manifest_path=external)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_rejects_nonfinite_json(
    constant: str,
) -> None:
    with pytest.raises(loader.PristineCommonIrError, match="non-finite JSON value"):
        loader._json_object(f'{{"value":{constant}}}'.encode(), label="synthetic")


@pytest.mark.parametrize(
    ("target", "value", "message"),
    [
        ("artifact_role", "review", "artifact role"),
        ("generator_version", "unreviewed", "automatic allowlist"),
        ("parser", "unreviewed", "automatic allowlist"),
        ("parser_version", "unreviewed", "automatic allowlist"),
    ],
)
def test_rejects_unreviewed_producer_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    value: str,
    message: str,
) -> None:
    archive, manifest = _fixture(tmp_path)
    _pin_fixture(monkeypatch, archive, manifest)

    document = _document(IDS[0], "pdf", "a" * 64)
    if target == "artifact_role":
        document["document"][target] = value  # type: ignore[index]
    else:
        document["document"]["provenance"][target] = value  # type: ignore[index]
    with pytest.raises(loader.PristineCommonIrError, match=message):
        loader._validate_document(
            document,
            notice_id=IDS[0],
            expected={"format": "pdf", "source_sha256": "a" * 64},
        )


def test_rejects_schema_invalid_common_ir() -> None:
    document = _document(IDS[0], "pdf", "a" * 64)
    document["blocks"] = {"not": "an array"}

    with pytest.raises(loader.PristineCommonIrError, match="schema validation failed"):
        loader._validate_document(
            document,
            notice_id=IDS[0],
            expected={"format": "pdf", "source_sha256": "a" * 64},
        )
