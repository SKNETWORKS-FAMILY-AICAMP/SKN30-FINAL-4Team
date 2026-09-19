#!/usr/bin/env python3
"""Generate and execute the offline OpenDataLoader coordinate calibration.

The command never mutates Common IR, selector inputs, database rows or the
production ``opendataloader_artifact/v1`` contract.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Mapping
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SRC = REPOSITORY_ROOT / "backend" / "vendor" / "common_ir_pipeline" / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (  # noqa: E402
    OpenDataLoaderCoordinateCalibrationError,
    build_calibration_proof,
    build_parser_run_manifest,
    generate_fixture_suite,
    read_regular_bytes,
    validate_fixture_suite,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (  # noqa: E402
    PdfRenderManifest,
    PdfRenderManifestError,
    validate_render_manifest_files,
)


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    raw = read_regular_bytes(path)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OpenDataLoaderCoordinateCalibrationError(f"failed to decode {role}") from error
    if not isinstance(value, dict):
        raise OpenDataLoaderCoordinateCalibrationError(f"{role} must be a JSON object")
    return value


def _safe_existing_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError("Java executable does not exist") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise OpenDataLoaderCoordinateCalibrationError("Java executable must be an executable regular file")
    return resolved


def _safe_existing_regular(path: Path, *, role: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError(f"{role} does not exist") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise OpenDataLoaderCoordinateCalibrationError(f"{role} must be a regular file")
    return resolved


def _safe_below(root: Path, relative: object, *, role: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise OpenDataLoaderCoordinateCalibrationError(f"{role} must be a relative path")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / relative).resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise OpenDataLoaderCoordinateCalibrationError(f"{role} escapes the fixture root") from error
    return resolved


def _find_single_json(output: Path) -> Path:
    candidates = sorted(path for path in output.rglob("*.json") if path.is_file() and not path.is_symlink())
    if len(candidates) != 1:
        raise OpenDataLoaderCoordinateCalibrationError(
            f"parser must emit exactly one JSON file per fixture; found {len(candidates)}"
        )
    return candidates[0]


def _java_identity(java_executable: Path, java_package: Path) -> tuple[Path, str, str, str]:
    executable = _safe_existing_executable(java_executable)
    package = _safe_existing_regular(java_package, role="provided Java package file")
    try:
        completed = subprocess.run(
            [str(executable), "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OpenDataLoaderCoordinateCalibrationError("java -version timed out") from error
    version_output = completed.stdout.strip()
    if completed.returncode != 0 or 'version "17.' not in version_output:
        raise OpenDataLoaderCoordinateCalibrationError("calibration requires a working Java 17 runtime")
    return (
        executable,
        sha256(read_regular_bytes(executable, maximum_bytes=128 * 1024 * 1024)).hexdigest(),
        sha256(read_regular_bytes(package, maximum_bytes=128 * 1024 * 1024)).hexdigest(),
        version_output,
    )


def _parser_identity(parser_jar: Path, package_metadata: Path) -> tuple[Path, str, str]:
    jar_path = _safe_existing_regular(parser_jar, role="parser JAR")
    jar = read_regular_bytes(jar_path, maximum_bytes=128 * 1024 * 1024)
    expected_entries = {
        "META-INF/maven/org.opendataloader/opendataloader-pdf-cli/pom.properties": "opendataloader-pdf-cli",
        "META-INF/maven/org.opendataloader/opendataloader-pdf-core/pom.properties": "opendataloader-pdf-core",
    }
    try:
        with zipfile.ZipFile(BytesIO(jar)) as archive:
            names = archive.namelist()
            for entry, artifact_id in expected_entries.items():
                if names.count(entry) != 1:
                    raise OpenDataLoaderCoordinateCalibrationError("parser JAR version metadata is missing or duplicated")
                info = archive.getinfo(entry)
                if info.file_size < 1 or info.file_size > 4096:
                    raise OpenDataLoaderCoordinateCalibrationError("parser JAR version metadata is unbounded")
                properties = archive.read(info).decode("utf-8")
                required = {
                    "groupId=org.opendataloader",
                    f"artifactId={artifact_id}",
                    "version=2.5.7",
                }
                if not required.issubset({line.strip() for line in properties.splitlines()}):
                    raise OpenDataLoaderCoordinateCalibrationError("parser JAR is not OpenDataLoader 2.5.7")
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as error:
        raise OpenDataLoaderCoordinateCalibrationError("failed to verify parser JAR identity") from error
    metadata = read_regular_bytes(package_metadata, maximum_bytes=1024 * 1024)
    try:
        metadata_lines = {line.strip() for line in metadata.decode("utf-8").splitlines()}
    except UnicodeDecodeError as error:
        raise OpenDataLoaderCoordinateCalibrationError("parser package metadata is not UTF-8") from error
    if not {"Name: opendataloader-pdf", "Version: 2.5.7"}.issubset(metadata_lines):
        raise OpenDataLoaderCoordinateCalibrationError("parser package metadata is not OpenDataLoader 2.5.7")
    return jar_path, sha256(jar).hexdigest(), sha256(metadata).hexdigest()


def _run_parser(
    parser_jar: Path,
    source_pdf: Path,
    output: Path,
    *,
    timeout_seconds: int,
    java_executable: Path,
) -> bytes:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    command = [
        str(java_executable),
        "-Djava.awt.headless=true",
        "-Dapple.awt.UIElement=true",
        "-jar",
        str(parser_jar),
        "--hybrid",
        "off",
        "--format",
        "json",
        "--output-dir",
        str(output),
        str(source_pdf),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env={
                **os.environ,
                "PATH": str(java_executable.parent) + os.pathsep + os.environ.get("PATH", ""),
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            },
        )
    except subprocess.TimeoutExpired as error:
        raise OpenDataLoaderCoordinateCalibrationError("OpenDataLoader calibration run timed out") from error
    if completed.returncode != 0:
        tail = completed.stdout[-2000:].replace("\x00", "")
        raise OpenDataLoaderCoordinateCalibrationError(
            f"OpenDataLoader calibration run failed with exit {completed.returncode}: {tail}"
        )
    return read_regular_bytes(_find_single_json(output))


def _write_json_exclusive(path: Path, value: Mapping[str, Any], *, role: str) -> None:
    rendered = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError(f"failed to publish {role}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verified_fixture_cases(
    fixture_root: Path,
    suite: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Path, PdfRenderManifest]]:
    """Validate the complete suite and bind every parser input before writes."""
    cases = validate_fixture_suite(suite)
    verified: list[tuple[Mapping[str, Any], Path, PdfRenderManifest]] = []
    for case in cases:
        source_pdf = _safe_below(fixture_root, case["source_pdf_relative_path"], role="source PDF path")
        source_bytes = read_regular_bytes(source_pdf, maximum_bytes=1024 * 1024)
        if (
            sha256(source_bytes).hexdigest() != case["source_pdf_sha256"]
            or len(source_bytes) != case["source_pdf_size_bytes"]
        ):
            raise OpenDataLoaderCoordinateCalibrationError("fixture source PDF binding is invalid")
        manifest_path = _safe_below(
            fixture_root,
            case["render_manifest_relative_path"],
            role="render manifest path",
        )
        try:
            manifest = PdfRenderManifest.from_dict(_load_json(manifest_path, role="render manifest"))
            validate_render_manifest_files(manifest, artifact_root=manifest_path.parent)
        except PdfRenderManifestError as error:
            raise OpenDataLoaderCoordinateCalibrationError("fixture render manifest is invalid") from error
        manifest_source = (manifest_path.parent / manifest.source_pdf_relative_path).resolve(strict=True)
        try:
            same_source = os.path.samefile(source_pdf, manifest_source)
        except OSError as error:
            raise OpenDataLoaderCoordinateCalibrationError("failed to bind fixture source PDF") from error
        if (
            not same_source
            or manifest.source_pdf_sha256 != case["source_pdf_sha256"]
            or manifest.source_pdf_size_bytes != case["source_pdf_size_bytes"]
            or len(manifest.pages) != 1
            or manifest.manifest_sha256() != case["render_manifest_sha256"]
        ):
            raise OpenDataLoaderCoordinateCalibrationError("fixture render manifest binding is invalid")
        coordinate = manifest.pages[0].coordinate_manifest
        if coordinate.manifest_sha256() != case["coordinate_manifest_sha256"]:
            raise OpenDataLoaderCoordinateCalibrationError("fixture coordinate manifest binding is invalid")
        verified.append((case, source_pdf, manifest))
    return verified


def _assert_source_unchanged(source_pdf: Path, case: Mapping[str, Any]) -> None:
    source = read_regular_bytes(source_pdf, maximum_bytes=1024 * 1024)
    if sha256(source).hexdigest() != case["source_pdf_sha256"] or len(source) != case["source_pdf_size_bytes"]:
        raise OpenDataLoaderCoordinateCalibrationError("fixture source PDF changed during parser execution")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = args.fixture_root.resolve(strict=True)
    suite = _load_json(fixture_root / "fixture_suite.json", role="fixture suite")
    verified_cases = _verified_fixture_cases(fixture_root, suite)
    parser_jar, parser_jar_sha256, parser_metadata_sha256 = _parser_identity(
        args.parser_jar,
        args.parser_package_metadata,
    )
    java_executable, java_executable_sha256, java_package_sha256, java_version_output = _java_identity(
        args.java_executable,
        args.java_package,
    )
    output_root = args.output_root
    output_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    fixture_suite_sha256 = sha256(
        json.dumps(suite, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    case_inputs = {}
    for case, source_pdf, manifest in verified_cases:
        case_id = case["case_id"]
        coordinate = manifest.pages[0].coordinate_manifest
        runs: list[tuple[bytes, Mapping[str, Any]]] = []
        for run_id in ("run-1", "run-2"):
            _assert_source_unchanged(source_pdf, case)
            case_output = output_root / run_id / case_id
            raw_output = _run_parser(
                parser_jar,
                source_pdf,
                case_output / "parser-output",
                timeout_seconds=args.timeout_seconds,
                java_executable=java_executable,
            )
            _assert_source_unchanged(source_pdf, case)
            run_manifest = build_parser_run_manifest(
                run_id=run_id,
                case=case,
                fixture_suite_sha256=fixture_suite_sha256,
                raw_output=raw_output,
                parser_jar_sha256=parser_jar_sha256,
                parser_package_metadata_sha256=parser_metadata_sha256,
                java_executable_sha256=java_executable_sha256,
                java_provided_package_file_sha256=java_package_sha256,
                java_version_output=java_version_output,
            )
            _write_json_exclusive(case_output / "run_manifest.json", run_manifest, role="parser run manifest")
            runs.append((raw_output, run_manifest))
        first, run_manifest_1 = runs[0]
        second, run_manifest_2 = runs[1]
        case_inputs[case_id] = (coordinate, first, second, run_manifest_1, run_manifest_2)
    # Detect producer replacement during the multi-run calibration before a
    # trusted proof is published.
    parser_identity_after = _parser_identity(args.parser_jar, args.parser_package_metadata)
    java_identity_after = _java_identity(args.java_executable, args.java_package)
    if parser_identity_after[1:] != (parser_jar_sha256, parser_metadata_sha256):
        raise OpenDataLoaderCoordinateCalibrationError("parser identity changed during calibration")
    if java_identity_after[1:] != (java_executable_sha256, java_package_sha256, java_version_output):
        raise OpenDataLoaderCoordinateCalibrationError("Java identity changed during calibration")
    proof = build_calibration_proof(
        suite,
        case_inputs,
        parser_jar_sha256=parser_jar_sha256,
        parser_package_metadata_sha256=parser_metadata_sha256,
        java_executable_sha256=java_executable_sha256,
        java_provided_package_file_sha256=java_package_sha256,
        java_version_output=java_version_output,
    )
    _write_json_exclusive(output_root / "calibration_proof.json", proof, role="calibration proof")
    return proof


def evaluate_existing(args: argparse.Namespace) -> dict[str, Any]:
    """Re-evaluate two producer-bound parser runs without executing Java."""
    fixture_root = args.fixture_root.resolve(strict=True)
    run_1_root = args.run_1_root.resolve(strict=True)
    run_2_root = args.run_2_root.resolve(strict=True)
    try:
        if os.path.samefile(run_1_root, run_2_root):
            raise OpenDataLoaderCoordinateCalibrationError("parser replay roots must be distinct")
    except OSError as error:
        raise OpenDataLoaderCoordinateCalibrationError("failed to compare parser replay roots") from error
    suite = _load_json(fixture_root / "fixture_suite.json", role="fixture suite")
    verified_cases = _verified_fixture_cases(fixture_root, suite)
    _parser_jar, parser_jar_sha256, parser_metadata_sha256 = _parser_identity(
        args.parser_jar,
        args.parser_package_metadata,
    )
    _java_executable, java_executable_sha256, java_package_sha256, java_version_output = _java_identity(
        args.java_executable,
        args.java_package,
    )
    case_inputs = {}
    for case, _source_pdf, manifest in verified_cases:
        case_id = case["case_id"]
        coordinate = manifest.pages[0].coordinate_manifest
        run_1_case_root = run_1_root / case_id
        run_2_case_root = run_2_root / case_id
        run_1 = read_regular_bytes(_find_single_json(run_1_case_root / "parser-output"))
        run_2 = read_regular_bytes(_find_single_json(run_2_case_root / "parser-output"))
        run_manifest_1 = _load_json(run_1_case_root / "run_manifest.json", role="parser run manifest 1")
        run_manifest_2 = _load_json(run_2_case_root / "run_manifest.json", role="parser run manifest 2")
        case_inputs[case_id] = (coordinate, run_1, run_2, run_manifest_1, run_manifest_2)
    proof = build_calibration_proof(
        suite,
        case_inputs,
        parser_jar_sha256=parser_jar_sha256,
        parser_package_metadata_sha256=parser_metadata_sha256,
        java_executable_sha256=java_executable_sha256,
        java_provided_package_file_sha256=java_package_sha256,
        java_version_output=java_version_output,
    )
    _write_json_exclusive(args.proof_output, proof, role="calibration proof")
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("generate", help="generate deterministic fixture PDFs and renders")
    generate.add_argument("--output-root", type=Path, required=True)
    run = subcommands.add_parser("execute", help="run ODL twice and emit the calibration proof")
    run.add_argument("--fixture-root", type=Path, required=True)
    run.add_argument("--parser-jar", type=Path, required=True)
    run.add_argument("--parser-package-metadata", type=Path, required=True)
    run.add_argument("--java-executable", type=Path, required=True)
    run.add_argument("--java-package", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=int, default=120)
    evaluate = subcommands.add_parser("evaluate", help="evaluate two completed ODL run directories")
    evaluate.add_argument("--fixture-root", type=Path, required=True)
    evaluate.add_argument("--run-1-root", type=Path, required=True)
    evaluate.add_argument("--run-2-root", type=Path, required=True)
    evaluate.add_argument("--parser-jar", type=Path, required=True)
    evaluate.add_argument("--parser-package-metadata", type=Path, required=True)
    evaluate.add_argument("--java-executable", type=Path, required=True)
    evaluate.add_argument("--java-package", type=Path, required=True)
    evaluate.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_fixture_suite(args.output_root)
            summary = {
                "fixture_root": str(args.output_root),
                "fixture_suite_sha256": sha256(
                    json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "case_count": len(result["cases"]),
            }
        elif args.command == "execute":
            if args.timeout_seconds < 1 or args.timeout_seconds > 600:
                parser.error("--timeout-seconds must be between 1 and 600")
            proof = execute(args)
            summary = {
                "proof": str(args.output_root / "calibration_proof.json"),
                "status": proof["status"],
                "selected_global_convention": proof["selected_global_convention"],
            }
        else:
            proof = evaluate_existing(args)
            summary = {
                "proof": str(args.proof_output),
                "status": proof["status"],
                "selected_global_convention": proof["selected_global_convention"],
            }
    except (OpenDataLoaderCoordinateCalibrationError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
