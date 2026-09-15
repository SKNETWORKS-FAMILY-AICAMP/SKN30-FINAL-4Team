"""Discover field candidates by original IR block without generating values or summaries."""

from __future__ import annotations

import json
import os
import argparse
import re
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from semantic_structuring.pipeline import _openai_schema
from semantic_structuring.notice_preparation import (
    SectionScopeDecision,
    classify_attachment_scopes,
    prepare_notice,
    section_scope_payload,
)
from semantic_structuring.common_ir_v1 import (
    apply_common_ir_v1_section_scopes,
    classify_common_ir_v1_attachment_scopes,
    common_ir_v1_section_scope_payload,
    common_ir_v1_identity,
    prepare_common_ir_v1,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateRoute(StrEnum):
    PURPOSE = "purpose"
    TARGET_OR_RESTRICTION = "target_or_restriction"
    SUPPORT = "support"
    FUNDING_OR_CONDITION = "funding_or_condition"
    DELIVERY = "delivery"
    APPLICATION_OPERATION = "application_operation"
    TABLE = "table"
    NOT_RELEVANT = "not_relevant"


class TableDisposition(StrEnum):
    """Whether a table enters A extraction or stays in B/C raw handling."""

    A_FACT_CANDIDATE = "a_fact_candidate"
    B_SEARCH_ONLY = "b_search_only"
    C_EXCLUDE = "c_exclude"


class BlockRouteCandidate(StrictModel):
    """A broad processing route over a canonical source block."""

    source_block_id: str
    route_tags: list[CandidateRoute] = Field(min_length=1)
    table_disposition: TableDisposition | None = None


class BlockCandidateDiscovery(StrictModel):
    notice_id: str
    candidate_pack_id: str
    candidates: list[BlockRouteCandidate] = Field(default_factory=list)


# Bump this together with any routing instruction/response-contract change.
ROUTER_CONTRACT_VERSION = "router-v0.3-table-disposition-complete"


_RETAINED_SECTION_SCOPES = {"main_notice", "substantive_attachment", "mixed_or_unresolved"}


def _section_scope_by_block(scope_artifact: str | Path, *, notice_id: str) -> dict[str, str]:
    """Read completed section-scope ranges into a canonical block map.

    Form templates and operational guides are intentionally removed before the
    broad router.  A mixed section stays in the input because ambiguity must
    preserve searchability, not cause a silent drop.
    """

    payload = json.loads(Path(scope_artifact).read_text())
    if payload.get("notice_id") != notice_id:
        raise ValueError("section-scope artifact belongs to a different notice")
    scopes: dict[str, str] = {}
    for section in payload.get("sections", []):
        start, end = section["source_block_range"]
        match_start, match_end = re.fullmatch(r"body\[(\d+)\]", start), re.fullmatch(r"body\[(\d+)\]", end)
        if not match_start or not match_end:
            raise ValueError("section-scope artifact has an invalid source-block range")
        for index in range(int(match_start.group(1)), int(match_end.group(1)) + 1):
            block_id = f"body[{index}]"
            if block_id in scopes:
                raise ValueError("section-scope artifact contains overlapping ranges")
            scopes[block_id] = section["scope"]
    return scopes


def _router_blocks_after_section_scope(pack, scope_artifact: str | Path):
    """Keep A candidates from the main notice and B tables from attachments.

    ``substantive_attachment`` and ``mixed_or_unresolved`` remain intact in
    common IR/vector search.  Their narrative blocks are deliberately not fed
    to the A-field router: otherwise a B-only attachment leaks back into an A
    candidate just because it mentions an amount or condition.
    """

    scope_by_block = _section_scope_by_block(scope_artifact, notice_id=pack.notice_id)
    if {block.block_id for block in pack.blocks} - set(scope_by_block):
        raise ValueError("section-scope artifact does not cover every input block")
    return [
        block
        for block in pack.blocks
        if scope_by_block[block.block_id] == "main_notice"
        or (scope_by_block[block.block_id] in _RETAINED_SECTION_SCOPES and block.block_kind == "table")
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", default="PBLN_000000000125002")
    parser.add_argument("--common-ir", type=Path, help="production Common IR v1 input")
    parser.add_argument("--section-scope-artifact")
    parser.add_argument("--apply-section-scope", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.section_scope_artifact and args.apply_section_scope:
        raise ValueError("use either --section-scope-artifact or --apply-section-scope, not both")

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")

    section_scope_usage = None
    section_scopes = None
    generated_scope_artifact = None
    if args.common_ir:
        document = json.loads(args.common_ir.read_text())
        unscoped, projection = prepare_common_ir_v1(document)
        notice_id = projection.notice_id
        if args.notice_id != "PBLN_000000000125002" and args.notice_id != notice_id:
            raise ValueError("--notice-id does not match Common IR")
        if args.apply_section_scope:
            decisions, section_scope_usage = classify_common_ir_v1_attachment_scopes(OpenAI(), document)
            prepared = apply_common_ir_v1_section_scopes(document, decisions)
            pack = prepared.router_pack()
            section_scopes = [{"section_id": section.section_id, "scope": section.scope} for section in prepared.sections]
            scope_payload = common_ir_v1_section_scope_payload(prepared, identity=common_ir_v1_identity(document))
            scope_payload["usage"] = section_scope_usage.__dict__
            generated_scope_artifact = Path(f"semantic_structuring/results/common_ir_v1/{notice_id}_{projection.source_kind}_section_scopes.json")
            generated_scope_artifact.parent.mkdir(parents=True, exist_ok=True)
            generated_scope_artifact.write_text(json.dumps(scope_payload, ensure_ascii=False, indent=2))
        if args.section_scope_artifact:
            scope_payload = json.loads(Path(args.section_scope_artifact).read_text())
            if (scope_payload.get("notice_id") != notice_id or scope_payload.get("input_contract") != "common_ir_v1"
                    or scope_payload.get("common_ir_identity") != common_ir_v1_identity(document)):
                raise ValueError("section-scope artifact does not match this Common IR")
            decisions = [
                SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
                for item in scope_payload["sections"] if item.get("scope") != "main_notice"
            ]
            prepared = apply_common_ir_v1_section_scopes(document, decisions)
            pack = prepared.router_pack()
    else:
        ir_path = f"exploratory_study/results/schema_validation/hangul_ir/{args.notice_id}.json"
        document = json.loads(Path(ir_path).read_text())
        if args.apply_section_scope:
            decisions, section_scope_usage = classify_attachment_scopes(OpenAI(), document)
            prepared = prepare_notice(document, decisions)
            pack = prepared.router_pack()
            section_scopes = [{"section_id": section.candidate.section_id, "scope": section.scope} for section in prepared.sections]
            scope_payload = section_scope_payload(prepared)
            scope_payload["usage"] = section_scope_usage.__dict__
            generated_scope_artifact = Path(f"semantic_structuring/results/{args.notice_id}_section_scopes_latest.json")
            generated_scope_artifact.write_text(json.dumps(scope_payload, ensure_ascii=False, indent=2))
        if args.section_scope_artifact:
            scope_payload = json.loads(Path(args.section_scope_artifact).read_text())
            if scope_payload.get("notice_id") != args.notice_id:
                raise ValueError("section-scope artifact belongs to a different notice")
            decisions = [
                SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
                for item in scope_payload["sections"]
                if item["scope"] != "main_notice"
            ]
            pack = prepare_notice(document, decisions).router_pack()
    if not args.apply_section_scope and not args.section_scope_artifact:
        parser.error("--section-scope-artifact or --apply-section-scope is required; legacy full-document routing is disabled")
    instructions = (
        f"Route common-IR source blocks for a Korean public-support notice under {ROUTER_CONTRACT_VERSION}. This is a high-recall routing pass, not field extraction. "
        "Use purpose for business purpose or goal; target_or_restriction for applicant, beneficiary, eligibility, exclusion, or duplicate-support limitation; "
        "support for support methods, items, amount, rate, selection count, or support/agreement/performance period; "
        "funding_or_condition for cost sharing, payment, settlement, or obligations after selection; delivery for organizations or partners and their roles; "
        "application_operation for application/submission/contact/operational information; use not_relevant for headings or boilerplate that fits no other route. "
        "Return exactly one candidate for every supplied source block. Every input with block_kind=table must receive only table and a table_disposition. "
        "Use a_fact_candidate only for a concise, directly usable current comparison table: it must be necessary to populate a core fact such as applicant/beneficiary, support item/method, amount/rate/selection count/period, cost sharing, payment condition, or explicit restriction. "
        "Use b_search_only for a table whose primary purpose is detailed program/round/stage description, evaluation/process/reference/technical detail, a full eligibility or exclusion reference list, organization/contact lists, catalogs, schedules, or any table useful for retrieval but too detailed to flatten into the A comparison profile. "
        "A table remains b_search_only when it incidentally contains money, counts, eligibility, or restrictions but its main function is one of those detailed/reference uses. "
        "Use c_exclude for blank forms, submission templates, contact-only/application-operation tables, and writing guides. "
        "For non-table blocks table_disposition must be null. "
        "Do not resolve conflicts, infer missing relations, decide which duplicate wins, or extract exact schema fields. "
        "Return only canonical source_block_id values and route_tags. Do not output source text, quotations, summaries, values, normalized numbers, "
        "organizations, or any new interpretation."
    )
    request = {
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "source_blocks": [block.model_dump(mode="json") for block in pack.blocks],
    }
    response = OpenAI().responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
        store=False,
        input=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "block_candidate_discovery",
                "strict": True,
                "schema": _openai_schema(BlockCandidateDiscovery),
            }
        },
    )
    result = BlockCandidateDiscovery.model_validate_json(response.output_text)
    blocks_by_id = {block.block_id: block for block in pack.blocks}
    available_ids = set(blocks_by_id)
    candidate_ids = [item.source_block_id for item in result.candidates]
    unknown_ids = sorted(set(candidate_ids) - available_ids)
    missing_ids = sorted(available_ids - set(candidate_ids))
    duplicate_ids = sorted({block_id for block_id in candidate_ids if candidate_ids.count(block_id) > 1})
    if result.notice_id != pack.notice_id or result.candidate_pack_id != pack.pack_id or unknown_ids or missing_ids or duplicate_ids:
        raise RuntimeError(f"invalid discovery identifiers: unknown={unknown_ids} missing={missing_ids} duplicate={duplicate_ids}")
    for item in result.candidates:
        is_table = blocks_by_id[item.source_block_id].block_kind == "table"
        if is_table and (item.route_tags != [CandidateRoute.TABLE] or item.table_disposition is None):
            raise RuntimeError(f"table route requires only table and table_disposition: {item.source_block_id}")
        if not is_table and item.table_disposition is not None:
            raise RuntimeError(f"non-table route must not have table_disposition: {item.source_block_id}")

    usage = response.usage
    payload = {
        "router_contract_version": ROUTER_CONTRACT_VERSION,
        "result": result.model_dump(mode="json"),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cached_input_tokens": usage.input_tokens_details.cached_tokens,
        },
        "input_block_count": len(pack.blocks),
        "input_block_ids": [block.block_id for block in pack.blocks],
        "section_scope_artifact": args.section_scope_artifact,
        "section_scope_usage": section_scope_usage.__dict__ if section_scope_usage else None,
        "section_scopes": section_scopes,
        "generated_section_scope_artifact": str(generated_scope_artifact) if generated_scope_artifact else None,
    }
    artifact_suffix = "_section_filtered" if (args.section_scope_artifact or args.apply_section_scope) else ""
    artifact_path = args.output or Path(
        f"semantic_structuring/results/common_ir_v1/{pack.notice_id}_{projection.source_kind}_block_candidates{artifact_suffix}.json"
        if args.common_ir else f"semantic_structuring/results/{args.notice_id}_block_candidates{artifact_suffix}_latest.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({**payload, "artifact": str(artifact_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
