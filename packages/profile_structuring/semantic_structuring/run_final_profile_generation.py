"""Deprecated legacy SemanticExtraction runner.

Do not use this as the production profile path: it predates the one-call,
source-selection contract and remains only for historical experiment reading.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.candidate_assembly import build_routed_a_pack
from semantic_structuring.notice_preparation import SectionScopeDecision, prepare_notice
from semantic_structuring.run_block_candidate_discovery_test import ROUTER_CONTRACT_VERSION


def build_pack(notice_id: str):
    ir_path = Path(f"exploratory_study/results/schema_validation/hangul_ir/{notice_id}.json")
    scope_path = Path(f"semantic_structuring/results/{notice_id}_section_scopes_latest.json")
    router_path = Path(f"semantic_structuring/results/{notice_id}_block_candidates_section_filtered_latest.json")
    document = json.loads(ir_path.read_text())
    scope_payload = json.loads(scope_path.read_text())
    router_payload = json.loads(router_path.read_text())
    if scope_payload.get("notice_id") != notice_id:
        raise ValueError("section-scope artifact belongs to a different notice")
    if router_payload.get("router_contract_version") != ROUTER_CONTRACT_VERSION:
        raise ValueError("router artifact contract version is stale or unsupported")
    decisions = [
        SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
        for item in scope_payload["sections"]
        if item["scope"] != "main_notice"
    ]
    prepared = prepare_notice(document, decisions)
    if router_payload.get("result", {}).get("notice_id") != notice_id:
        raise ValueError("router artifact belongs to a different notice")
    pack, _ = build_routed_a_pack(prepared, router_payload["result"]["candidates"])
    if pack is None:
        raise RuntimeError(f"{notice_id}: no A candidate blocks")
    return pack


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("notice_ids", nargs="+", default=["PBLN_000000000125016", "PBLN_000000000125056", "PBLN_000000000125612"])
    parser.add_argument("--output-dir", default="semantic_structuring/results/final_profiles")
    args = parser.parse_args()
    raise SystemExit(
        "Deprecated: run_source_selection_test.py is the only supported one-call profile extraction runner. "
        "This legacy SemanticExtraction runner is intentionally disabled."
    )


if __name__ == "__main__":
    main()
