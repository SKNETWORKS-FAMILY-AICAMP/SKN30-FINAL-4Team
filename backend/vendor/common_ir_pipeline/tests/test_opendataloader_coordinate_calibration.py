from __future__ import annotations

from importlib.resources import files
import importlib.util
import json
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.opendataloader_artifact import COORDINATE_SPACES_V1
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    CASE_SPECS,
    CONFIG_SHA256,
    FIXTURE_SCHEMA_VERSION,
    OpenDataLoaderCoordinateCalibrationError,
    PROOF_SCHEMA_VERSION,
    build_calibration_pdf,
    build_calibration_proof,
    build_parser_run_manifest,
    validate_fixture_suite,
    _score_convention,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    validate_render_manifest_files,
)
from common_ir_pipeline.workers.pdfium_renderer import resolve_pdf_page_geometries


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BASELINE = (
    REPOSITORY_ROOT
    / "backend"
    / "baselines"
    / "pdf_reconstruction"
    / "opendataloader_coordinate_calibration_v1"
)


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _cli_module():
    path = REPOSITORY_ROOT / "backend" / "scripts" / "run_opendataloader_coordinate_calibration.py"
    spec = importlib.util.spec_from_file_location("coordinate_calibration_cli_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schemas() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    root = files("common_ir_pipeline.pdf_fusion").joinpath("schemas")
    return (
        json.loads(root.joinpath("opendataloader_coordinate_fixture_suite_v1.schema.json").read_text("utf-8")),
        json.loads(root.joinpath("opendataloader_coordinate_calibration_v1.schema.json").read_text("utf-8")),
        json.loads(root.joinpath("opendataloader_coordinate_parser_run_v1.schema.json").read_text("utf-8")),
    )


def _baseline_inputs(
    suite: dict[str, object],
    *,
    alter_second_raw: bool = False,
) -> dict[str, tuple[object, bytes, bytes]]:
    result = {}
    cases = suite["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        case_id = case["case_id"]
        assert isinstance(case_id, str)
        manifest_path = BASELINE / str(case["render_manifest_relative_path"])
        manifest = PdfRenderManifest.from_dict(_json(manifest_path))
        validate_render_manifest_files(manifest, artifact_root=manifest_path.parent)
        run_1_root = BASELINE / "odl-run-1" / case_id
        run_2_root = BASELINE / "odl-run-2" / case_id
        first = (run_1_root / "parser-output" / "calibration.json").read_bytes()
        second = (run_2_root / "parser-output" / "calibration.json").read_bytes()
        if alter_second_raw and case_id == "combined-r270-u2":
            second += b"\n"
        result[case_id] = (
            manifest.pages[0].coordinate_manifest,
            first,
            second,
            _json(run_1_root / "run_manifest.json"),
            _json(run_2_root / "run_manifest.json"),
        )
    return result


def test_calibration_pdfs_cover_rotation_crop_and_userunit() -> None:
    assert {spec.rotation for spec in CASE_SPECS} == {0, 90, 180, 270}
    assert {spec.user_unit for spec in CASE_SPECS} == {1, 2}
    assert any(spec.media_box != spec.crop_box for spec in CASE_SPECS)
    for spec in CASE_SPECS:
        encoded, anchors = build_calibration_pdf(spec)
        geometry = resolve_pdf_page_geometries(encoded)
        assert len(geometry) == 1
        assert geometry[0].media_box == tuple(float(value) for value in spec.media_box)
        assert geometry[0].crop_box == tuple(float(value) for value in spec.crop_box)
        assert geometry[0].rotation == spec.rotation
        assert geometry[0].user_unit == float(spec.user_unit)
        assert len(anchors) == 5
        assert len({anchor["label"] for anchor in anchors}) == 5


def test_frozen_suite_and_machine_bound_proof_validate_and_replay() -> None:
    suite_schema, proof_schema, run_schema = _schemas()
    Draft202012Validator.check_schema(suite_schema)
    Draft202012Validator.check_schema(proof_schema)
    Draft202012Validator.check_schema(run_schema)
    suite = _json(BASELINE / "fixture_suite.json")
    proof = _json(BASELINE / "calibration_proof.json")
    Draft202012Validator(suite_schema).validate(suite)
    Draft202012Validator(proof_schema).validate(proof)
    for run_id in ("odl-run-1", "odl-run-2"):
        for path in (BASELINE / run_id).glob("*/run_manifest.json"):
            Draft202012Validator(run_schema).validate(_json(path))
    assert suite["schema_version"] == FIXTURE_SCHEMA_VERSION
    assert proof["schema_version"] == PROOF_SCHEMA_VERSION
    parser = proof["parser"]
    runtime = proof["runtime"]
    assert isinstance(parser, dict)
    assert isinstance(runtime, dict)
    rebuilt = build_calibration_proof(
        suite,
        _baseline_inputs(suite),
        parser_jar_sha256=str(parser["jar_sha256"]),
        parser_package_metadata_sha256=str(parser["package_metadata_sha256"]),
        java_executable_sha256=str(runtime["executable_sha256"]),
        java_provided_package_file_sha256=str(runtime["provided_package_file_sha256"]),
        java_version_output=str(runtime["version_output"]),
    )
    assert rebuilt == proof
    assert rebuilt["status"] == "passed"
    assert rebuilt["producer_identity_verified"] is True
    assert rebuilt["selected_global_convention"] == "rotated_crop_relative_bottom_left_raw_units"
    assert all(row["deterministic_replay"] for row in rebuilt["case_results"])
    assert max(
        hypothesis["max_center_error_pt"]
        for row in rebuilt["case_results"]
        for hypothesis in row["hypotheses"]
        if hypothesis["convention"] == rebuilt["selected_global_convention"]
    ) < 1.0


def test_repeat_byte_difference_fails_the_proof_even_when_json_is_equivalent() -> None:
    suite = _json(BASELINE / "fixture_suite.json")
    proof = _json(BASELINE / "calibration_proof.json")
    parser = proof["parser"]
    runtime = proof["runtime"]
    assert isinstance(parser, dict)
    assert isinstance(runtime, dict)
    inputs = _baseline_inputs(suite, alter_second_raw=True)
    cases = suite["cases"]
    assert isinstance(cases, list)
    case = next(row for row in cases if isinstance(row, dict) and row.get("case_id") == "combined-r270-u2")
    coordinate, first, second, run_manifest_1, _old_run_manifest_2 = inputs["combined-r270-u2"]
    inputs["combined-r270-u2"] = (
        coordinate,
        first,
        second,
        run_manifest_1,
        build_parser_run_manifest(
            run_id="run-2",
            case=case,
            fixture_suite_sha256=str(proof["fixture_suite_sha256"]),
            raw_output=second,
            parser_jar_sha256=str(parser["jar_sha256"]),
            parser_package_metadata_sha256=str(parser["package_metadata_sha256"]),
            java_executable_sha256=str(runtime["executable_sha256"]),
            java_provided_package_file_sha256=str(runtime["provided_package_file_sha256"]),
            java_version_output=str(runtime["version_output"]),
        ),
    )
    rebuilt = build_calibration_proof(
        suite,
        inputs,
        parser_jar_sha256=str(parser["jar_sha256"]),
        parser_package_metadata_sha256=str(parser["package_metadata_sha256"]),
        java_executable_sha256=str(runtime["executable_sha256"]),
        java_provided_package_file_sha256=str(runtime["provided_package_file_sha256"]),
        java_version_output=str(runtime["version_output"]),
    )
    assert rebuilt["status"] == "failed"
    combined = next(row for row in rebuilt["case_results"] if row["case_id"] == "combined-r270-u2")
    assert combined["deterministic_replay"] is False
    assert combined["status"] == "failed"


def test_existing_v1_coordinate_contract_remains_unverified() -> None:
    assert COORDINATE_SPACES_V1 == frozenset({"odl_pdf_points_unverified"})
    assert CONFIG_SHA256 == "8543138f8508f6f6ebee3280edd010778b3c7b800161cad3d7ef93cdb011cd82"


def test_suite_rejects_a_missing_calibration_case() -> None:
    suite = _json(BASELINE / "fixture_suite.json")
    suite["cases"] = list(suite["cases"][:-1])
    proof = _json(BASELINE / "calibration_proof.json")
    parser = proof["parser"]
    runtime = proof["runtime"]
    assert isinstance(parser, dict)
    assert isinstance(runtime, dict)
    try:
        build_calibration_proof(
            suite,
            _baseline_inputs(_json(BASELINE / "fixture_suite.json")),
            parser_jar_sha256=str(parser["jar_sha256"]),
            parser_package_metadata_sha256=str(parser["package_metadata_sha256"]),
            java_executable_sha256=str(runtime["executable_sha256"]),
            java_provided_package_file_sha256=str(runtime["provided_package_file_sha256"]),
            java_version_output=str(runtime["version_output"]),
        )
    except OpenDataLoaderCoordinateCalibrationError as error:
        assert "exact v1 case set" in str(error)
    else:
        raise AssertionError("missing fixture case was accepted")


def test_suite_oracle_and_paths_must_match_the_deterministic_generator() -> None:
    suite = _json(BASELINE / "fixture_suite.json")
    first = suite["cases"][0]
    assert isinstance(first, dict)
    first["source_pdf_relative_path"] = "cases/crop-r0-u1/source/calibration.pdf"

    try:
        validate_fixture_suite(suite)
    except OpenDataLoaderCoordinateCalibrationError as error:
        assert "deterministic generator" in str(error)
    else:
        raise AssertionError("modified fixture oracle/path was accepted")


def test_cached_raw_output_cannot_be_relabelled_with_an_old_run_manifest() -> None:
    suite = _json(BASELINE / "fixture_suite.json")
    proof = _json(BASELINE / "calibration_proof.json")
    parser = proof["parser"]
    runtime = proof["runtime"]
    assert isinstance(parser, dict)
    assert isinstance(runtime, dict)
    inputs = _baseline_inputs(suite, alter_second_raw=True)

    try:
        build_calibration_proof(
            suite,
            inputs,
            parser_jar_sha256=str(parser["jar_sha256"]),
            parser_package_metadata_sha256=str(parser["package_metadata_sha256"]),
            java_executable_sha256=str(runtime["executable_sha256"]),
            java_provided_package_file_sha256=str(runtime["provided_package_file_sha256"]),
            java_version_output=str(runtime["version_output"]),
        )
    except OpenDataLoaderCoordinateCalibrationError as error:
        assert "run manifest binding" in str(error)
    else:
        raise AssertionError("tampered cached raw output was accepted")


def test_bbox_extent_gate_rejects_center_preserving_shrink() -> None:
    suite = _json(BASELINE / "fixture_suite.json")
    cases = suite["cases"]
    assert isinstance(cases, list)
    case = next(row for row in cases if isinstance(row, dict) and row.get("case_id") == "crop-r0-u1")
    manifest_path = BASELINE / str(case["render_manifest_relative_path"])
    coordinate = PdfRenderManifest.from_dict(_json(manifest_path)).pages[0].coordinate_manifest
    crop_x0, crop_y0, _crop_x1, _crop_y1 = coordinate.crop_box
    observed = {}
    anchors = case["anchors"]
    assert isinstance(anchors, list)
    for anchor in anchors:
        assert isinstance(anchor, dict)
        expected = anchor["expected_text_bbox_user"]
        assert isinstance(expected, list)
        center_x = (expected[0] + expected[2]) / 2
        center_y = (expected[1] + expected[3]) / 2
        half_width = (expected[2] - expected[0]) * 0.05
        half_height = (expected[3] - expected[1]) * 0.05
        observed[str(anchor["anchor_id"])] = (
            center_x - half_width - crop_x0,
            center_y - half_height - crop_y0,
            center_x + half_width - crop_x0,
            center_y + half_height - crop_y0,
        )

    score = _score_convention(
        "crop_relative_bottom_left_raw_units",
        observed,
        anchors,
        coordinate=coordinate,
    )

    assert score["max_center_error_pt"] == 0.0
    assert score["max_edge_error_pt"] > 1.0
    assert score["min_bbox_iou"] < 0.95
    assert score["status"] == "rejected"


def test_cli_preflight_rejects_source_bytes_that_do_not_match_the_suite(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    shutil.copy2(BASELINE / "fixture_suite.json", fixture_root / "fixture_suite.json")
    shutil.copytree(BASELINE / "cases", fixture_root / "cases")
    source = fixture_root / "cases" / "baseline-r0-u1" / "source" / "calibration.pdf"
    source.write_bytes(source.read_bytes() + b"\n")
    suite = _json(fixture_root / "fixture_suite.json")

    try:
        _cli_module()._verified_fixture_cases(fixture_root, suite)
    except OpenDataLoaderCoordinateCalibrationError as error:
        assert "source PDF binding" in str(error)
    else:
        raise AssertionError("source bytes different from the suite were accepted")
