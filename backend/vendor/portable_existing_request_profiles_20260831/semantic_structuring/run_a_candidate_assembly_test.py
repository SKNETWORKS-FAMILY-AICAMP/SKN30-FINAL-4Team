"""Inspect A-field candidate packs after common-IR preparation and routing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from semantic_structuring.candidate_assembly import build_routed_a_pack
from semantic_structuring.run_block_candidate_discovery_test import ROUTER_CONTRACT_VERSION
from semantic_structuring.notice_preparation import SectionScopeDecision, prepare_notice


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", default="PBLN_000000000125608")
    parser.add_argument("--section-scope-artifact", required=True)
    parser.add_argument("--router-artifact", required=True)
    args = parser.parse_args()

    ir_path = Path(f"exploratory_study/results/schema_validation/hangul_ir/{args.notice_id}.json")
    document = json.loads(ir_path.read_text())
    scope_payload = json.loads(Path(args.section_scope_artifact).read_text())
    router_payload = json.loads(Path(args.router_artifact).read_text())
    if scope_payload["notice_id"] != args.notice_id or router_payload["result"]["notice_id"] != args.notice_id:
        raise ValueError("input artifacts belong to a different notice")
    if router_payload.get("router_contract_version") != ROUTER_CONTRACT_VERSION:
        raise ValueError("router artifact contract version is stale or unsupported")
    decisions = [
        SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
        for item in scope_payload["sections"]
        if item["scope"] != "main_notice"
    ]
    prepared = prepare_notice(document, decisions)
    combined_pack, table_metrics = build_routed_a_pack(prepared, router_payload["result"]["candidates"])
    payload = {
        "notice_id": args.notice_id,
        "table_metrics": table_metrics,
        "combined_pack": {
            "pack_id": combined_pack.pack_id,
            "source_block_ids": [block.block_id for block in combined_pack.blocks],
            "block_count": len(combined_pack.blocks),
        }
        if combined_pack
        else None,
    }
    artifact_path = Path(f"semantic_structuring/results/{args.notice_id}_a_candidate_packs_latest.json")
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({**payload, "artifact": str(artifact_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
