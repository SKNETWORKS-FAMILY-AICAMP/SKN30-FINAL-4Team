"""Build final profile JSON files from existing source-selection artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from semantic_structuring.final_profile_assembler import assemble_final_profile, assemble_final_profile_v02
from semantic_structuring.common_ir_v1 import common_ir_v1_metadata, prepare_common_ir_v1


def _metadata_path(notice_id: str) -> Path:
    paired_hwp = Path(f"exploratory_study/input/paired_hwp/{notice_id}/metadata.json")
    return paired_hwp if paired_hwp.exists() else Path(f"exploratory_study/input/paired/{notice_id}/metadata.json")


def _selection_path(notice_id: str) -> Path:
    v3 = Path(f"semantic_structuring/results/{notice_id}_source_selection_v0.3_latest.json")
    v2 = Path(f"semantic_structuring/results/{notice_id}_source_selection_v0.2_latest.json")
    candidates = [path for path in (v3, v2) if path.exists()]
    if not candidates:
        raise FileNotFoundError(f"no source-selection artifact for {notice_id}")
    # A failed retry can leave a syntactically valid but nearly empty v0.3 file.
    # Select the richer accepted artifact; this is selection-artifact choice, not
    # a semantic merge of two runs.
    return max(candidates, key=lambda path: len(json.loads(path.read_text()).get("materialized_evidence", [])))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("notice_ids", nargs="*", default=[])
    parser.add_argument("--output-dir", default="semantic_structuring/results/assembled_final_profiles")
    parser.add_argument("--common-ir", type=Path, help="assemble one independent Common IR v1 result")
    parser.add_argument("--selection-artifact", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile-version", choices=("v0.1", "v0.2"), default="v0.1")
    args = parser.parse_args()
    if bool(args.common_ir) != bool(args.selection_artifact):
        parser.error("--common-ir and --selection-artifact must be supplied together")
    if args.common_ir:
        document = json.loads(args.common_ir.read_text())
        _, projection = prepare_common_ir_v1(document)
        selection_artifact = json.loads(args.selection_artifact.read_text())
        if args.profile_version == "v0.1" and selection_artifact.get("selection_contract") == "v0.2_anchor":
            raise ValueError("v0.2_anchor selection artifacts cannot be assembled as v0.1 profiles")
        assemble = assemble_final_profile_v02 if args.profile_version == "v0.2" else assemble_final_profile
        if args.profile_version == "v0.2":
            profile = assemble(
                selection_artifact,
                common_ir_v1_metadata(document),
                derived_projections=[
                    *selection_artifact.get("support_facets", []),
                    *selection_artifact.get("support_scale_measures", []),
                ],
            )
        else:
            profile = assemble(selection_artifact, common_ir_v1_metadata(document))
        output_path = args.output or Path(
            f"semantic_structuring/results/common_ir_v1/{projection.notice_id}_{projection.source_kind}_structured_profile.{args.profile_version}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"notice_id": projection.notice_id, "source_kind": projection.source_kind, "output": str(output_path)}, ensure_ascii=False))
        return
    if not args.notice_ids:
        parser.error("notice_ids are required unless --common-ir and --selection-artifact are supplied")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for notice_id in args.notice_ids:
        selection_path = _selection_path(notice_id)
        selection_artifact = json.loads(selection_path.read_text())
        if args.profile_version == "v0.1" and selection_artifact.get("selection_contract") == "v0.2_anchor":
            raise ValueError("v0.2_anchor selection artifacts cannot be assembled as v0.1 profiles")
        assemble = assemble_final_profile_v02 if args.profile_version == "v0.2" else assemble_final_profile
        profile = assemble(
            selection_artifact,
            json.loads(_metadata_path(notice_id).read_text()),
        )
        output_path = output_dir / f"{notice_id}.{args.profile_version}.json"
        output_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"notice_id": notice_id, "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
