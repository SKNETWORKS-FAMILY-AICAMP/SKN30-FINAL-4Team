from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "freeze_pristine_hard6_common_ir.py"
SPEC = importlib.util.spec_from_file_location("freeze_pristine_hard6_common_ir", SCRIPT)
assert SPEC and SPEC.loader
freezer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freezer
SPEC.loader.exec_module(freezer)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _common_ir(notice_id: str, source_format: str, source_hash: str, *, source_location: str) -> dict[str, object]:
    document: dict[str, object] = {
        "document_id": f"{source_format}:{notice_id}",
        "source_kind": source_format,
        "artifact_role": "production",
        "provenance": {
            "method": "pdf_native_only" if source_format == "pdf" else "rhwp",
            "page": None,
            "bbox": None,
            "coordinate_space": None,
            "source_location": source_location,
            "source_sha256": source_hash,
            "generator": "common_ir_v1_adapters",
            "generator_version": "1.1.0",
            "schema_version": "common_ir_v1",
            "parser": "pdf_inspector" if source_format == "pdf" else "rhwp",
            "parser_version": "1.17.0" if source_format == "pdf" else "0.8.1",
        },
    }
    if source_format == "pdf":
        document.update(
            {
                "page_count": 1,
                "native_text_page_count": 1,
                "pdf_semantic_eligibility": "eligible_native_text",
                "pdf_semantic_reason": "substantive_native_text_available",
            }
        )
    return {"schema_version": "common_ir_v1", "document": document, "blocks": [], "relations": [], "conflicts": []}


def _fixture_roots(tmp_path: Path) -> dict[str, Path]:
    inputs = tmp_path / "inputs"
    pdf_a = tmp_path / "pdf-a"
    pdf_b = tmp_path / "pdf-b"
    inventory: list[dict[str, object]] = []
    hwp_documents: list[dict[str, object]] = []
    for notice_id, source_format in freezer.EXPECTED_NOTICES:
        source = (f"{notice_id}-{source_format}-source").encode("utf-8")
        source_path = inputs / notice_id / f"source.{source_format}"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)
        source_hash = sha256(source).hexdigest()
        inventory.append(
            {
                "notice_id": notice_id,
                "format": source_format,
                "input_path": str(source_path),
                "source_sha256": source_hash,
            }
        )
        document = _common_ir(
            notice_id,
            source_format,
            source_hash,
            source_location=("source.pdf" if source_format == "pdf" else str(source_path)),
        )
        if source_format == "pdf":
            raw = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
            native = f"native-{notice_id}".encode("utf-8")
            for root in (pdf_a, pdf_b):
                run = root / notice_id
                run.mkdir(parents=True, exist_ok=True)
                (run / "source.pdf").write_bytes(source)
                (run / "native.json").write_bytes(native)
                (run / "common_ir.json").write_bytes(raw)
                _write_json(
                    run / "manifest.json",
                    {
                        "notice_id": notice_id,
                        "artifacts": {
                            "source_pdf": {"sha256": source_hash},
                            "native_capture": {"sha256": sha256(native).hexdigest()},
                            "common_ir": {"sha256": sha256(raw).hexdigest()},
                        },
                    },
                )
        else:
            hwp_documents.append(document)
    inventory_path = tmp_path / "input_manifest.json"
    _write_json(inventory_path, {"notices": inventory})
    hwp_a = tmp_path / "hwp-a.json"
    hwp_b = tmp_path / "hwp-b.json"
    hwp_raw = json.dumps(hwp_documents[0], ensure_ascii=False, sort_keys=True).encode("utf-8")
    hwp_a.write_bytes(hwp_raw)
    hwp_b.write_bytes(hwp_raw)
    return {"inventory": inventory_path, "pdf_a": pdf_a, "pdf_b": pdf_b, "hwp_a": hwp_a, "hwp_b": hwp_b}


def _build(paths: dict[str, Path]) -> tuple[bytes, dict[str, object]]:
    return freezer.build_freeze(
        source_inventory=paths["inventory"],
        pdf_replay_root_a=paths["pdf_a"],
        pdf_replay_root_b=paths["pdf_b"],
        hwp_common_ir_a=paths["hwp_a"],
        hwp_common_ir_b=paths["hwp_b"],
    )


def test_builds_deterministic_six_member_archive_and_public_text_free_manifest(tmp_path: Path) -> None:
    paths = _fixture_roots(tmp_path)
    first_archive, first_manifest = _build(paths)
    second_archive, second_manifest = _build(paths)

    assert first_archive == second_archive
    assert first_manifest == second_manifest
    assert first_manifest["archive_sha256"] == sha256(first_archive).hexdigest()
    assert [item["notice_id"] for item in first_manifest["notices"]] == list(freezer.EXPECTED_IDS)  # type: ignore[index]
    assert "-pdf-source" not in json.dumps(first_manifest, ensure_ascii=False)
    with ZipFile(BytesIO(first_archive)) as archive:
        assert archive.namelist() == [
            f"{notice_id}/pipeline/common_ir_v1/{notice_id}.{source_format}.json"
            for notice_id, source_format in freezer.EXPECTED_NOTICES
        ]
        hwp_member = f"PBLN_000000000117175/pipeline/common_ir_v1/PBLN_000000000117175.hwp.json"
        hwp_document = json.loads(archive.read(hwp_member))
        assert hwp_document["document"]["provenance"]["source_location"] == "source.hwp"


def test_verifier_requires_canonical_public_manifest_and_exact_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture_roots(tmp_path)
    archive, manifest = _build(paths)
    archive_path = tmp_path / "hard6.zip"
    manifest_path = tmp_path / "hard6.json"
    expected_path = tmp_path / "reviewed-baseline.json"
    archive_path.write_bytes(archive)
    manifest_path.write_bytes(freezer._canonical_json(manifest))
    expected_path.write_bytes(freezer._canonical_json(manifest))
    monkeypatch.setattr(freezer, "DEFAULT_EXPECTED_MANIFEST", expected_path)

    freezer.verify_freeze(archive_path=archive_path, manifest_path=manifest_path, expected_manifest=expected_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(freezer.Hard6FreezeError, match="not canonical"):
        freezer.verify_freeze(archive_path=archive_path, manifest_path=manifest_path, expected_manifest=expected_path)

    changed_inventory = json.loads(paths["inventory"].read_text(encoding="utf-8"))
    changed_inventory["notices"][0]["source_sha256"] = "0" * 64
    _write_json(paths["inventory"], changed_inventory)
    with pytest.raises(freezer.Hard6FreezeError, match="source SHA-256 mismatch"):
        _build(paths)


def test_external_expected_manifest_cannot_replace_checked_in_trust_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture_roots(tmp_path)
    archive, manifest = _build(paths)
    archive_path = tmp_path / "hard6.zip"
    manifest_path = tmp_path / "hard6.json"
    reviewed_path = tmp_path / "checked-in.json"
    attacker_path = tmp_path / "attacker.json"
    archive_path.write_bytes(archive)
    manifest_path.write_bytes(freezer._canonical_json(manifest))
    reviewed_path.write_bytes(freezer._canonical_json(manifest))
    attacker = deepcopy(manifest)
    attacker["archive_sha256"] = "0" * 64
    attacker_path.write_bytes(freezer._canonical_json(attacker))
    monkeypatch.setattr(freezer, "DEFAULT_EXPECTED_MANIFEST", reviewed_path)

    with pytest.raises(freezer.Hard6FreezeError, match="checked-in reviewed baseline"):
        freezer.verify_freeze(
            archive_path=archive_path,
            manifest_path=manifest_path,
            expected_manifest=attacker_path,
        )


def test_pair_publication_rolls_back_archive_when_manifest_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "hard6.zip"
    manifest_path = tmp_path / "hard6.json"
    real_create = freezer._atomic_create

    def fail_manifest(path: Path, payload: bytes, *, label: str) -> None:
        if label == "public manifest":
            raise freezer.Hard6FreezeError("synthetic manifest publication failure")
        real_create(path, payload, label=label)

    monkeypatch.setattr(freezer, "_atomic_create", fail_manifest)

    with pytest.raises(freezer.Hard6FreezeError, match="synthetic manifest"):
        freezer._create_archive_manifest_pair(
            archive_path=archive_path,
            archive_payload=b"archive",
            manifest_path=manifest_path,
            manifest_payload=b"manifest",
        )

    assert not archive_path.exists()
    assert not manifest_path.exists()


def test_rejects_ab_disagreement_and_manual_adjudication_metadata(tmp_path: Path) -> None:
    paths = _fixture_roots(tmp_path)
    notice_id = "PBLN_000000000103645"
    raw = (paths["pdf_b"] / notice_id / "common_ir.json").read_bytes() + b"\n"
    (paths["pdf_b"] / notice_id / "common_ir.json").write_bytes(raw)
    manifest = json.loads((paths["pdf_b"] / notice_id / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["common_ir"]["sha256"] = sha256(raw).hexdigest()
    _write_json(paths["pdf_b"] / notice_id / "manifest.json", manifest)
    with pytest.raises(freezer.Hard6FreezeError, match="A/B byte identity mismatch"):
        _build(paths)

    paths = _fixture_roots(tmp_path / "native-mismatch")
    changed_native = b"different native bytes"
    (paths["pdf_b"] / notice_id / "native.json").write_bytes(changed_native)
    manifest = json.loads((paths["pdf_b"] / notice_id / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["native_capture"]["sha256"] = sha256(changed_native).hexdigest()
    _write_json(paths["pdf_b"] / notice_id / "manifest.json", manifest)
    with pytest.raises(freezer.Hard6FreezeError, match="native.json A/B byte identity mismatch"):
        _build(paths)

    paths = _fixture_roots(tmp_path / "manual")
    contaminated = json.loads((paths["hwp_a"]).read_text(encoding="utf-8"))
    contaminated["document"]["provenance"]["generator"] = "manual_gold_adjudication"
    raw = json.dumps(contaminated, ensure_ascii=False, sort_keys=True).encode("utf-8")
    paths["hwp_a"].write_bytes(raw)
    paths["hwp_b"].write_bytes(raw)
    with pytest.raises(freezer.Hard6FreezeError, match="manual adjudication provenance"):
        _build(paths)


def test_shared_guard_does_not_treat_source_text_as_manual_metadata() -> None:
    document = _common_ir("PBLN_000000000117175", "hwp", "a" * 64, source_location="source.hwp")
    safe = deepcopy(document)
    safe["blocks"] = [
        {
            "block_id": "b1",
            "kind": "paragraph",
            "structure_status": "explicit",
            "text": "manual gold adjudication is ordinary source text",
            "text_occurrence_ids": [],
            "reading_order": 0,
            "page": 1,
            "section_path": "body",
            "occurrences": [],
            "provenance": {"method": "rhwp", "page": 1, "bbox": None, "coordinate_space": None, "source_location": "raw"},
        }
    ]
    freezer.require_automatic_common_ir(safe)


def test_rejects_aliased_replays_nonportable_lineage_and_nonallowlisted_producer(tmp_path: Path) -> None:
    paths = _fixture_roots(tmp_path)
    with pytest.raises(freezer.Hard6FreezeError, match="roots A/B paths must not alias"):
        freezer.build_freeze(
            source_inventory=paths["inventory"],
            pdf_replay_root_a=paths["pdf_a"],
            pdf_replay_root_b=paths["pdf_a"],
            hwp_common_ir_a=paths["hwp_a"],
            hwp_common_ir_b=paths["hwp_b"],
        )

    paths = _fixture_roots(tmp_path / "hwp-alias")
    with pytest.raises(freezer.Hard6FreezeError, match="HWP Common IR A/B paths must not alias"):
        freezer.build_freeze(
            source_inventory=paths["inventory"],
            pdf_replay_root_a=paths["pdf_a"],
            pdf_replay_root_b=paths["pdf_b"],
            hwp_common_ir_a=paths["hwp_a"],
            hwp_common_ir_b=paths["hwp_a"],
        )

    paths = _fixture_roots(tmp_path / "pdf-hardlink")
    first_native = paths["pdf_a"] / "PBLN_000000000103645" / "native.json"
    second_native = paths["pdf_b"] / "PBLN_000000000103645" / "native.json"
    second_native.unlink()
    try:
        second_native.hardlink_to(first_native)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable on test filesystem: {error}")
    with pytest.raises(freezer.Hard6FreezeError, match="native.json A/B paths must not alias"):
        _build(paths)

    paths = _fixture_roots(tmp_path / "portable")
    hwp = json.loads(paths["hwp_a"].read_text(encoding="utf-8"))
    hwp["document"]["raw_artifact_ids"] = ["/private/raw.json"]
    raw = json.dumps(hwp, ensure_ascii=False, sort_keys=True).encode("utf-8")
    paths["hwp_a"].write_bytes(raw)
    paths["hwp_b"].write_bytes(raw)
    with pytest.raises(freezer.Hard6FreezeError, match="nonportable raw_artifact_ids"):
        _build(paths)

    paths = _fixture_roots(tmp_path / "producer")
    hwp = json.loads(paths["hwp_a"].read_text(encoding="utf-8"))
    hwp["document"]["provenance"]["generator"] = "unreviewed_adapter"
    raw = json.dumps(hwp, ensure_ascii=False, sort_keys=True).encode("utf-8")
    paths["hwp_a"].write_bytes(raw)
    paths["hwp_b"].write_bytes(raw)
    with pytest.raises(freezer.Hard6FreezeError, match="generator is not allowlisted"):
        _build(paths)


def test_rejects_same_output_path_and_archive_member_caps(tmp_path: Path) -> None:
    path = tmp_path / "same-output"
    with pytest.raises(freezer.Hard6FreezeError, match="paths must be distinct"):
        freezer._assert_distinct_paths(path, path, label="archive and public manifest")

    paths = _fixture_roots(tmp_path / "oversize")
    freezer.MAX_COMMON_IR_BYTES = 1
    try:
        with pytest.raises(freezer.Hard6FreezeError, match="exceeds size limit"):
            _build(paths)
    finally:
        freezer.MAX_COMMON_IR_BYTES = 64 * 1024 * 1024
