"""Run one A-profile source-selection extraction without LLM-written evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.anchor_occurrence_resolver import SHA256_HEX_PATTERN
from semantic_structuring.candidate_assembly import build_routed_a_pack, restrict_a_pack_to_block_ids, restrict_a_pack_to_table_ids
from semantic_structuring.notice_preparation import SectionScopeDecision, prepare_notice
from semantic_structuring.common_ir_v1 import apply_common_ir_v1_section_scopes, common_ir_v1_identity, prepare_common_ir_v1
from semantic_structuring.run_block_candidate_discovery_test import ROUTER_CONTRACT_VERSION
from semantic_structuring.pipeline import _openai_schema
from semantic_structuring.source_selection import (
    AnchorCorrectionRequest,
    AnchorCorrectionResponse,
    SourceSelectionExtractionV02,
    apply_finalize_with_fallback_v02,
    build_corrected_anchor_audit,
    build_numeric_candidates,
    classify_empty_repair_response_v02,
    derive_support_scale_measures_v02,
    materialize_components,
    materialize_evidence,
    memoize_anchor_correction_resolver,
    normalize_explicit_condition_variant_relations_v02,
    normalize_nested_support_scale_anchors_v02,
    normalize_explicit_sequential_components_v02,
    preserve_prior_server_validated_facts_v02,
    validate_component_structure_v02,
    validate_scale_measure_candidates_v02,
    validate_selection_quality_v02,
    validate_support_cap_completeness_v02,
)


def _correct_ambiguous_value_anchor(
    client: OpenAI, usages: list, request: AnchorCorrectionRequest
) -> str:
    """Ask the same structured-output model to pick one repeated-anchor span.

    Reuses the existing OpenAI structured-output call path. The prompt
    carries only ``candidate_id``, ``anchor_text``, and left/right context for
    each candidate -- never start/end offsets or an occurrence ordinal -- so
    the model must disambiguate from context alone. Any failure here (API,
    network, or an incomplete/malformed structured-output response) is left
    to propagate to the caller, which is responsible for turning it into a
    classified, non-``ValueError`` failure -- never a plain exception that
    could be mistaken for an ordinary selection-contract validation error.
    """

    response = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
        store=False,
        input=[
            {
                "role": "system",
                "content": (
                    "The requested value_anchor is repeated more than once in its source block. "
                    "Choose the one candidate_id whose context_before/context_after matches the "
                    "field this anchor was selected for. Decide only from anchor_text and context; "
                    "no position or occurrence order is given."
                ),
            },
            {"role": "user", "content": json.dumps(request.model_dump(mode="json"), ensure_ascii=False)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "anchor_correction_response",
                "strict": True,
                "schema": _openai_schema(AnchorCorrectionResponse),
            }
        },
    )
    usages.append(response.usage)
    return AnchorCorrectionResponse.model_validate_json(response.output_text).candidate_id


def trusted_common_ir_source_sha256(document: dict, pack) -> str:
    """Fail fast on a missing/mismatched/malformed Common IR source identity.

    Called only when Common IR input was supplied, so this must never
    silently disable CandidatePack Anchor Occurrence Resolver v1 for a
    Common IR pack: it raises immediately -- before any model call -- on a
    pack with no ``common_ir_document_id``, a ``document_id`` mismatch
    between the loaded Common IR document and the routed pack, or a
    ``source_sha256`` that is missing or not a lowercase 64-character
    SHA-256 hex digest.
    """

    if pack.common_ir_document_id is None:
        raise RuntimeError(
            "Common IR input was supplied but the routed candidate pack carries no common_ir_document_id"
        )
    identity = common_ir_v1_identity(document)
    if identity["document_id"] != pack.common_ir_document_id:
        raise RuntimeError(
            f"Common IR document_id {identity['document_id']!r} does not match the routed candidate pack's "
            f"common_ir_document_id {pack.common_ir_document_id!r}"
        )
    source_sha256 = identity["source_sha256"]
    if not isinstance(source_sha256, str) or not re.fullmatch(SHA256_HEX_PATTERN, source_sha256):
        raise RuntimeError(
            "Common IR source_sha256 is missing or not a lowercase 64-character SHA-256 hex digest"
        )
    return source_sha256


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", default="PBLN_000000000125608")
    parser.add_argument("--common-ir", type=Path, help="production Common IR v1 input")
    parser.add_argument("--section-scope-artifact")
    parser.add_argument("--router-artifact")
    parser.add_argument(
        "--table-id", action="append", default=[],
        help="limit this selection run to one or more Common IR table ids; table-only results require later server merge",
    )
    parser.add_argument(
        "--source-block-id", action="append", default=[],
        help="limit this selection run to explicit CandidatePack block ids; scoped results require later server merge",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selection-contract", choices=("v0.2_anchor",), default="v0.2_anchor")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")
    if not args.section_scope_artifact or not args.router_artifact:
        parser.error("--section-scope-artifact and --router-artifact are required")
    elif args.common_ir:
        document = json.loads(args.common_ir.read_text())
        _, projection = prepare_common_ir_v1(document)
        if args.notice_id != "PBLN_000000000125608" and args.notice_id != projection.notice_id:
            raise ValueError("--notice-id does not match Common IR")
        scope_payload = json.loads(Path(args.section_scope_artifact).read_text())
        router_payload = json.loads(Path(args.router_artifact).read_text())
        if (scope_payload.get("notice_id") != projection.notice_id or scope_payload.get("input_contract") != "common_ir_v1"
                or scope_payload.get("common_ir_identity") != common_ir_v1_identity(document)):
            raise ValueError("section-scope artifact does not match this Common IR")
        if router_payload.get("result", {}).get("notice_id") != projection.notice_id:
            raise ValueError("router artifact belongs to a different notice")
        if router_payload.get("router_contract_version") != ROUTER_CONTRACT_VERSION:
            raise ValueError("router artifact contract version is stale or unsupported")
        decisions = [
            SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
            for item in scope_payload["sections"] if item.get("scope") != "main_notice"
        ]
        prepared = apply_common_ir_v1_section_scopes(document, decisions)
        pack, pack_metrics = build_routed_a_pack(prepared, router_payload["result"]["candidates"])
    else:
        ir_path = Path(f"exploratory_study/results/schema_validation/hangul_ir/{args.notice_id}.json")
        document = json.loads(ir_path.read_text())
        scope_payload = json.loads(Path(args.section_scope_artifact).read_text())
        router_payload = json.loads(Path(args.router_artifact).read_text())
        if scope_payload.get("notice_id") != args.notice_id:
            raise ValueError("section-scope artifact belongs to a different notice")
        if router_payload.get("result", {}).get("notice_id") != args.notice_id:
            raise ValueError("router artifact belongs to a different notice")
        if router_payload.get("router_contract_version") != ROUTER_CONTRACT_VERSION:
            raise ValueError("router artifact contract version is stale or unsupported")
        decisions = [
            SectionScopeDecision.model_validate({"section_id": item["section_id"], "scope": item["scope"]})
            for item in scope_payload["sections"]
            if item["scope"] != "main_notice"
        ]
        prepared = prepare_notice(document, decisions)
        pack, pack_metrics = build_routed_a_pack(prepared, router_payload["result"]["candidates"])
    if pack is None:
        raise RuntimeError("no A candidate blocks available")
    if args.table_id and args.source_block_id:
        parser.error("use either --table-id or --source-block-id, not both")
    if args.table_id:
        pack = restrict_a_pack_to_table_ids(pack, args.table_id)
        pack_metrics["table_scope_ids"] = sorted(set(args.table_id))
        pack_metrics["table_scope_cell_total"] = len(pack.blocks)
    if args.source_block_id:
        pack = restrict_a_pack_to_block_ids(pack, args.source_block_id)
        pack_metrics["source_block_scope_ids"] = list(args.source_block_id)
        pack_metrics["source_block_scope_total"] = len(pack.blocks)

    # Trusted lineage for CandidatePack Anchor Occurrence Resolver v1: the
    # SHA-256 must be read from the loaded Common IR artifact itself, never
    # from the pack or a request. Only a Common IR v1 pack has this lineage,
    # so the resolver stays unused for the legacy notice-preparation path --
    # but a Common IR pack with mismatched, missing, or malformed lineage
    # fails fast here, before any model call, rather than silently running
    # the rest of this pass with the resolver disabled.
    common_ir_source_sha256 = (
        trusted_common_ir_source_sha256(document, pack) if args.common_ir else None
    )

    artifact_path = args.output or Path(
        f"semantic_structuring/results/common_ir_v1/{pack.notice_id}_{projection.source_kind}_source_selection_{args.selection_contract}.json"
        if args.common_ir else f"semantic_structuring/results/{args.notice_id}_source_selection_{args.selection_contract}.json"
    )
    failure_path = artifact_path.with_suffix(".failure.json")
    diagnostic_path = artifact_path.with_suffix(".run.jsonl")

    def diagnostic(event: str, **details: object) -> None:
        """Durable, secret-free progress trace for an interrupted run."""
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        with diagnostic_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **details}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    diagnostic("runner_ready", notice_id=pack.notice_id, artifact_path=str(artifact_path))

    def write_failure(error: Exception, attempts: int, last_validation_error: str | None) -> None:
        """Persist operational diagnostics without API keys or model text."""

        failure_path.parent.mkdir(parents=True, exist_ok=True)
        failure_path.write_text(json.dumps({
            "status": "failed",
            "notice_id": pack.notice_id,
            "candidate_pack_id": pack.pack_id,
            "selection_contract": args.selection_contract,
            "attempt_count": attempts,
            "error_type": type(error).__name__,
            # Present only for a classified failure (e.g. empty_repair_response);
            # null for the generic one-call contract-validation failure.
            "error_classification": getattr(error, "error_classification", None),
            "error_message": str(error),
            "last_validation_error": last_validation_error,
            # An audit-safe summary of the prior candidate an empty repair
            # response left behind, plus the error that prompted the repair.
            # Present only for empty_repair_response; never claims these
            # facts were server-validated.
            "prior_candidate_summary": getattr(error, "prior_candidate_summary", None),
            "prior_validation_error": getattr(error, "prior_validation_error", None),
            "artifact_path": str(artifact_path),
            "common_ir_identity": common_ir_v1_identity(document) if args.common_ir else None,
            "response_text_stored": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    instructions = (
        "Select A comparison-profile field claims from the supplied Korean public-support-notice source blocks. "
        "Return component_decision, support_components, and facts only. "
        "Never output source text, excerpts, value_raw, summaries, rewritten wording, or normalized numbers. "
        "For v0.2, use value_anchor for every fact: it has an exact input source_block_id and one exact, contiguous anchor_text copied from that block. "
        "The server—not you—will persist value_raw and its offsets. value_anchor must be unique in its block. "
        "Use value_anchor and primary_component_id. The v0.2 response contract contains no legacy block-value fields. "
        "context_source_block_ids are headers or surrounding cells used only as context. For a table, use an individual value cell paragraph as value_anchor and its row/column header as context. Never select a flattened [table ...] block as a fact value. "
        "A source block may support more than one independent field claim. "
        "Treat component discovery and fact selection as one coherent pass: first identify every explicit support package, selectable participation type, and benefit-bearing stage; then assign each package-specific fact to its owner. "
        "Use component_decision=mixed when a notice has more than one component kind (for example selectable participation types plus separate support packages). Do not discard named support packages merely because participation types also exist. "
        "A support package is an explicitly named beneficiary-and-benefit bundle. Create it even when its benefit is a wage, operating expense, service, or reimbursement rather than a distinct application track. "
        "For every such package, look for the direct beneficiary, eligible cost/service, support method/activity, actual support period, and each independent amount/rate/count. Do not leave those facts notice-scoped when their wording explicitly belongs to one package. "
        "When a notice lets an enterprise choose or combine multiple named support components, keep each component's own unit price/limit on that component. Put an explicitly common enterprise-wide cap, selectable-item maximum, or common cost rate at notice scope and set applicability_component_ids to every component it explicitly limits. Do not duplicate that same common cap into each component. A phrase such as '1개 기준' is only the applies-per-item context for its neighbouring amount, never an independent support_scale fact. "
        "Applicant, policy target, direct beneficiary, and calculation basis can be different. Preserve them as separate facts: an employer can apply while a worker receives the wage; a representative can apply while a team is selected; an enterprise can receive a reimbursement tied to a worker. "
        "For an employer-enterprise programme that pays the enterprise for hiring, mentoring, or employing a young person, use the participating enterprise as applicant_eligibility and support_target when it is the named participating/support recipient; use the young worker only as beneficiary with subject_role=policy_beneficiary. Do not duplicate the young-worker anchor as support_target and beneficiary. "
        "subject_role is only allowed on beneficiary facts and is optional. Use financial_recipient only when the source explicitly says that money, reimbursement, or payment goes to that entity; use policy_beneficiary only when the source explicitly identifies the person/entity whose employment, experience, service, or outcome the policy is intended to benefit. Leave it null when the distinction is not explicit. If both are explicit, create two beneficiary facts with separate exact anchors, and link the money/period/item facts with recipient_fact_ids to financial_recipient. Never use subject_role on applicant_eligibility, support_target, support_items, or another field. "
        "A beneficiary anchor must name an actual person, enterprise, or organization. Never use a calculation unit (such as 1인당 or 기업당) or a payment channel (such as 기업계좌 or 계좌 입금) as beneficiary. When a benefit is repeated in a summary and detailed section, create one support component and retain one non-duplicated scale fact; prefer the occurrence that explicitly identifies its beneficiary/package, while using the other only as context. "
        "For exclusions, anchor the restricted applicant, target, or condition itself. Never anchor only a generic outcome such as '대상에서 제외됨' or '신청 불가'. If that outcome is in a neighbouring block, use it only as context and select the adjacent exact condition span. "
        "Keep applicant_eligibility and a notice-wide selection capacity notice-scoped unless the source explicitly states that the application or capacity belongs only to one named component. Do not assign them to a component merely because the applicant later receives one package. "
        "When a support amount, selection capacity, service, or payment is explicitly for a beneficiary, create a beneficiary fact if one is present in the source and link the support fact using recipient_fact_ids. "
        "The same direct-recipient rule applies to support_items: when an item is a cost or service within a named beneficiary's package, link that item to the beneficiary with recipient_fact_ids. "
        "When an explicit labelled condition creates an alternative or subcase (for example 신규 채용 versus 기존 재직자, or a separate employment type), emit that condition as eligibility_conditions and use modifies_fact_ids to link it only to the amount/rate/limit it changes. "
        "For an eligibility_conditions fact, the value_anchor must contain the substantive condition itself. A short table label such as '(신규)', '(기존)', 'A형', or 'B형' is only context and must never be the value_anchor by itself; anchor the corresponding condition cell or sentence instead. "
        "Do not create a condition-to-support relation from proximity alone; the label, table row, or sentence must explicitly bind the condition and support value. "
        "purpose_goal is the business purpose; applicant_eligibility is only the actual submitting/applying/contracting entity itself, without its region, industry, size, certification, employment, or other qualifying conditions; support_target is the policy's basic target set; eligibility_conditions are the positive region, industry, size, certification, employment, or other qualifications that narrow the applicant or support target; beneficiary is who receives benefit; applicable_entity is a supported business/site/item; "
        "exclusions and duplicate_support_conditions are explicit restrictions; support_activities are what the beneficiary is supported to do; support_methods are delivery methods; support_items are concrete eligible costs/services; support_content is only for important support wording that cannot safely fit one of those fields. "
        "program_period is the notice/program's overall operating period, such as 사업기간, 사업수행기간, or 전체 추진기간; it is always notice-scoped and never a beneficiary's support duration. "
        "support_period is a duration actually provided to a selected enterprise, team, project, or beneficiary, such as 기업당 지원기간 or 선정기업 협약기간. It may be notice- or component-scoped depending on the explicit recipient/support bundle. "
        "Do not decide a period field from its heading alone. A period measured from 협약일, 협약 시작일, 선정일, or a selected participant's start date is support_period when it describes that participant's agreement or execution, even if a table labels the row 사업기간. Only emit program_period when the source establishes the programme's own official operating period independently of a selected participant's agreement. "
        "Do not classify 신청기간, 접수기간, 공고기간, or submission deadlines as either period field. If a duration has no explicit business subject or surrounding context that establishes whether it is the overall program or beneficiary support, do not emit a period fact. "
        "support_scale is a beneficiary/team/project-specific selection count, amount, rate, or support limit, but not a duration or an activity/session frequency. A phrase such as '직무교육 2회' or '멘토링 3회' is support_activities context, not support_scale. "
        "For support_scale and support_period, choose the smallest exact contiguous phrase that expresses exactly one value: for example, split '월 160만원 × 최대 6개월 (월급여 80% 이내)' into '월 160만원' (support_scale), '최대 6개월' (support_period), and '월급여 80% 이내' (support_scale). "
        "When an amount has an explicit payment or calculation cadence, keep that cadence in the same amount anchor: select '월 150만원' rather than '150만원', and '연 2.0%' rather than '2.0%'. The server derives frequency only from the verified source span. "
        "Likewise split a duration-plus-item phrase when both are explicit: select the duration as support_period and the eligible cost/service name as support_items; never let one replace the other. "
        "When a labelled support-period line belongs to the immediately following named beneficiary/package details, assign the labelled period to that package and do not also emit a shorter restatement of the same period. "
        "Never anchor an entire bullet, sentence, table cell, or conjunction when it combines a number with another amount, rate, duration, recipient limit, eligibility condition, or package. "
        "A support_scale fact must not contain a duration expression such as 개월/일/년 unless that expression is part of an unrelated context sentence; choose the numeric-scale substring instead. "
        "total_budget is the announced total budget; cost_sharing is participant-borne cost; "
        "payment_terms is payment or settlement; participation_requirements is an obligation to participate or perform after/while supported; "
        "delivery_roles are only explicit organization-to-role statements such as announcing, lead, dedicated, or operating agency. "
        "For delivery_roles, organization_anchors and role_anchor are mandatory. canonical_role is optional and may only be one of: announcing_agency, lead_agency, operating_agency, dedicated_agency, participating_partner, demand_partner, cooperating_organization; use null when none exactly applies. Each anchor has source_block_id and anchor_text. "
        "Every anchor_text must be an exact, contiguous phrase copied from its source block: organization anchors are the complete explicit organization name only; role_anchor is the complete explicit role phrase only, with no surrounding whitespace or Korean particles. "
        "Do not infer an organization role from co-occurrence, a joint-program sentence, a partner list, or an organization merely mentioned in an obligation. "
        "The server will locate and recover anchors from the source; anchors are not persisted as business values. Do not emit delivery_roles when an explicit organization name is absent. "
        "Return support_facets as comparison-only normalized projections in this same response. Each support_facets entry must reference one or more fact_id values returned in this response and must never create or rewrite Raw Fact text. "
        "Use activities for what is enabled (for example 창업교육, 일경험), methods for delivery form (for example 교육, 멘토링, 보조금, 융자, 보증, 바우처, 매칭), and items only for concrete costs or supplies. "
        "A Raw Fact may be support_items or support_content while its support_facets correctly classifies it as a method; do not duplicate the Raw Fact merely to produce a facet. Return an empty support_facets list when no support Fact is sufficiently clear. "
        "Always return support_scale_measures as an empty list. The server, not the model, deterministically derives amount/rate/count measures from verified support_scale spans after selection. "
        "Before finalizing facts, check every named support package/type/stage for explicit selection counts, amounts, rates, and caps such as 최대·한도. Select each as its own support_scale fact; do not omit a cap merely because another amount or period appears nearby. "
        "component_decision is mandatory: choose none, packages, participation_types, stages, or mixed. "
        "Choose none only when the source contains no explicit separate package, selectable type, or distinct stage; no_component_reason must then briefly state that controlled decision. "
        "If any components are returned, component_decision must match their kind (or mixed). "
        "Create support_components only when packages/types or stages differ in target, support, amount, or period. "
        "However, do not omit a component when the source explicitly presents alternative selectable participation types, separate named support packages, or sequential selection/education/final-support stages with different selection counts or benefits. "
        "For explicit alternatives, create one participation_type per alternative even when their core support is shared; keep shared support facts notice-scoped and use applicability_component_ids for type-specific facts. "
        "A stage_support is a recipient-and-benefit bundle, not every procedural box in a flow. Do not create a stage_support for a bare review, screening, or evaluation step with no education, money, mentoring, service, or other received benefit. "
        "When a review selects a cohort immediately for the following education/support step, attach that selection capacity to the following benefit stage. "
        "For distinct sequential benefit stages, create one stage_support per stage and attach the stage-specific capacity/benefit to it. "
        "Use support_package only for parallel, separately named benefit menus. Use stage_support for sequential benefit progression. In particular, education followed by final selection, a finalist grant, mentoring, or another later benefit is always one or more stage_support components, never support_package components. "
        "Each support component must include name_anchor when an exact package/type/stage name appears in its source. name_anchor has source_block_id and exact contiguous anchor_text; never write a component name yourself. "
        "For this cell-level input, table_block_ids must be an empty list: table parent identity is recovered by the server from selected cell ids. "
        "Link a fact owned by one package/type/stage with primary_component_id. "
        "Use applicability_component_ids when a fact is only true under an additional participation type or stage; do not create a combined component. "
        "Use modifies_fact_ids only for a fact that explicitly changes or limits another fact. "
        "For money/service facts, recipient_fact_ids point to the direct payment or settlement recipient; basis_fact_ids point to the person/entity whose employment, activity, equipment, or condition determines eligibility or calculation. "
        "Before returning, check every explicit support table or labelled support section once: independent value cells, cost/service names, beneficiaries, and conditional rows must either have their own fact or be omitted only because the source is genuinely not a support claim. "
        "Before returning, check every support component once: component-specific facts must use primary_component_id, and every explicit recipient relation must use recipient_fact_ids. "
        "For each support component, also inspect its immediately preceding/following descriptive paragraph and its table header/footnote for a shared support period or package-wide amount limit; emit those facts when explicit and attach them to that component. "
        "Use only fact ids returned in this same response; do not create a relationship from mere co-occurrence. "
        "purpose_goal requires an explicit purpose, objective, or aim statement; a notice title or a support-description heading is not a purpose by itself. "
        "duplicate_support_conditions requires an explicit duplicate/overlap-support restriction. A past-support history used only for priority or preference is not such a restriction. "
        "cost_sharing requires an explicit participant payment obligation, amount, or rate. A merely listed document containing words such as self-burden, receipt, or tax does not establish cost sharing. "
        "Do not create a claim solely for application instructions, contact details, submission document lists, evaluation schedules, reference-manual notices, or attachment lists. "
        "If a block is ambiguous, omit it rather than guessing."
    )
    numeric_candidates = build_numeric_candidates(pack)
    request = {
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "source_blocks": [block.model_dump(mode="json") for block in pack.blocks],
        "numeric_candidates": [candidate.model_dump(mode="json") for candidate in numeric_candidates],
    }
    # Do not leave a batch run indefinitely ambiguous when a provider accepts
    # a request but never returns an HTTP response.  The default remains
    # intentionally generous for long Korean notices and can be overridden
    # per run without changing the contract.
    client = OpenAI(timeout=float(os.environ.get("OPENAI_REQUEST_TIMEOUT_SECONDS", "180")))
    usages = []
    last_error: str | None = None
    prior_selection: dict | None = None
    carry_forward_extraction: SourceSelectionExtractionV02 | None = None
    component_normalizations: list[dict[str, str]] = []
    condition_variant_normalizations: list[dict[str, str]] = []
    preserved_fact_normalizations: list[dict[str, str]] = []
    preservation_fallback: dict[str, str] | None = None
    derived_scale_measures = []

    def _required_support_scale_anchors(validation_error: str | None) -> list[dict[str, str]]:
        """Expose only server-identified missing exact caps to a repair call."""
        marker = "select it as its own atomic support_scale span: "
        if not validation_error or marker not in validation_error:
            return []
        result = []
        for item in validation_error.split(marker, 1)[1].split(", "):
            block_id, separator, anchor_text = item.partition(": ")
            if separator and block_id and anchor_text:
                result.append({"source_block_id": block_id, "anchor_text": anchor_text})
        return result

    # Correction-call bookkeeping, scoped to this whole runner execution (not
    # to one attempt or one _finalize call): correction_usages is folded into
    # the final usage totals; correction_audit records safe, candidate_id-free
    # per-fact metadata for build_corrected_anchor_audit. The resolver itself
    # is memoized by (source_block_id, anchor_text) -- the deterministic
    # resolution's own identity -- so apply_finalize_with_fallback_v02's
    # merged/unmerged re-run can never call the correction model twice for
    # what is otherwise the same repeated anchor.
    correction_usages: list = []
    correction_audit: dict[str, dict[str, object]] = {}
    _memoized_correction_resolver = memoize_anchor_correction_resolver(
        lambda request: _correct_ambiguous_value_anchor(client, correction_usages, request)
    )

    def _resolve_ambiguous_value_anchor(request: AnchorCorrectionRequest) -> str:
        correction_audit[request.fact_id] = {
            "source_block_id": request.source_block_id,
            "candidate_count": len(request.candidates),
        }
        return _memoized_correction_resolver(request)

    def _finalize(candidate: SourceSelectionExtractionV02):
        """Validate, normalize, and materialize one candidate selection."""

        validate_selection_quality_v02(candidate)
        candidate, seq_normalizations = normalize_explicit_sequential_components_v02(candidate, pack)
        candidate, nested_scale_normalizations = normalize_nested_support_scale_anchors_v02(candidate)
        seq_normalizations.extend(nested_scale_normalizations)
        validate_component_structure_v02(candidate, pack)
        validate_support_cap_completeness_v02(candidate, pack)
        candidate, variant_normalizations = normalize_explicit_condition_variant_relations_v02(candidate)
        evidence = materialize_evidence(
            candidate,
            pack,
            common_ir_source_sha256=common_ir_source_sha256,
            resolve_ambiguous_value_anchor=(
                _resolve_ambiguous_value_anchor if common_ir_source_sha256 else None
            ),
        )
        components = materialize_components(candidate, pack)
        resolved_value_sources = {
            row.fact_id: row.value_source for row in evidence if row.value_source is not None
        }
        measures = derive_support_scale_measures_v02(
            candidate,
            numeric_candidates,
            resolved_value_sources,
            source_block_texts={block.block_id: block.text for block in pack.blocks},
        )
        # Validation runs here, before this candidate can ever reach a
        # written success artifact: it re-checks that every derived measure
        # binds inside its fact's own resolved value_source span (never by
        # anchor_text containment) and that no numerically-explicit
        # support_scale fact was left unmeasured.
        validate_scale_measure_candidates_v02(candidate, measures, numeric_candidates, resolved_value_sources)
        return candidate, seq_normalizations, variant_normalizations, evidence, components, measures

    # A rejected output gets one repair attempt.  It receives its own prior
    # structured response plus server-side contract failures, not a hidden
    # desired answer or Gold fixture.  Without the prior response a retry is
    # merely another stochastic extraction pass and cannot reliably repair an
    # invalid anchor or relationship.  Fixing the model's own text is not
    # enough: a one-call retry can silently drop an unrelated, already-valid
    # fact while it fixes the flagged problem, so the server also carries
    # forward whatever individually server-validated facts survive from the
    # last rejected attempt and merges them back in before re-validating.
    # Preservation itself must never turn an otherwise-valid attempt into a
    # hard failure, so the merge is finalized with a deterministic fallback
    # to the unmerged attempt.
    try:
        for attempt in range(2):
            request_payload = request
            retry_instruction = ""
            is_repair_attempt = bool(last_error) and prior_selection is not None
            if is_repair_attempt:
                required_caps = _required_support_scale_anchors(last_error)
                retry_instruction = (
                    " This is a repair attempt. The user payload contains previous_selection and "
                    "server_validation_errors. Return a complete revised selection. Preserve every valid "
                    "component, fact, exact anchor, and relationship from previous_selection. Change only "
                    "what is necessary to resolve the listed errors; do not re-extract the notice from scratch, "
                    "invent a business answer, or use any Gold/expected output."
                )
                if required_caps:
                    retry_instruction += (
                        " The payload's required_support_scale_anchors were deterministically found in "
                        "your own selected evidence. Include each one as a separate support_scale fact using "
                        "exactly its supplied source_block_id and anchor_text."
                    )
                request_payload = {
                    **request,
                    "previous_selection": prior_selection,
                    "server_validation_errors": [last_error],
                    "required_support_scale_anchors": required_caps,
                }
            diagnostic("selection_request_started", attempt=attempt + 1, repair=is_repair_attempt)
            response = client.responses.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
                reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
                store=False,
                input=[
                    {"role": "system", "content": instructions + retry_instruction},
                    {"role": "user", "content": json.dumps(request_payload, ensure_ascii=False)},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "source_selection_extraction",
                        "strict": True,
                        "schema": _openai_schema(SourceSelectionExtractionV02),
                    }
                },
            )
            diagnostic("selection_response_received", attempt=attempt + 1)
            usages.append(response.usage)
            extraction = SourceSelectionExtractionV02.model_validate_json(response.output_text)
            prior_selection = extraction.model_dump(mode="json")
            if extraction.notice_id != pack.notice_id or extraction.candidate_pack_id != pack.pack_id:
                last_error = "source selection output belongs to a different candidate pack"
                continue
            # A first attempt returning zero facts is an ordinary validation
            # failure (below) and gets the one planned repair attempt.  A
            # *repair* attempt returning zero facts is different: there is
            # nothing to merge, preserve, or assemble a profile from, and
            # retrying again would just be an unplanned extra model call.
            # Fail immediately and deterministically, carrying an audit-safe
            # summary of the prior (already-rejected) candidate -- never
            # claimed to have been server-validated -- and the error that
            # prompted this repair attempt.
            empty_repair_error = classify_empty_repair_response_v02(
                is_repair_attempt=is_repair_attempt,
                facts=extraction.facts,
                prior_candidate=carry_forward_extraction,
                prior_validation_error=last_error,
            )
            if empty_repair_error is not None:
                raise empty_repair_error
            # An A-pack is already routed as potentially substantive.  An entirely
            # empty response is therefore not a safe interpretation and is exposed
            # as a failed one-call extraction.  This does not prescribe any answer
            # or field value.
            if not extraction.facts:
                last_error = (
                    "the routed A candidate pack contains substantive source blocks, but no fact was selected. "
                    "Return every supported A claim and omit only genuinely unsupported blocks"
                )
                continue
            # `merged_extraction` is the raw, pre-server-normalization
            # candidate (this attempt's own facts plus whatever prior facts
            # were carried forward).  It -- not a post-normalization result
            # -- becomes the next attempt's carry-forward, so a server
            # normalization (e.g. an added modifies_fact_ids relation) is
            # never silently represented as a model-selected fact.
            merged_extraction, preserved_changes = preserve_prior_server_validated_facts_v02(
                carry_forward_extraction, extraction, pack
            )
            carry_forward_extraction = merged_extraction
            try:
                (
                    (extraction, normalizations, variant_normalizations, evidence, components, measures),
                    preserved_fact_normalizations,
                    preservation_fallback,
                ) = apply_finalize_with_fallback_v02(merged_extraction, extraction, preserved_changes, _finalize)
                component_normalizations = normalizations
                condition_variant_normalizations = variant_normalizations
                derived_scale_measures = measures
                diagnostic("selection_validated", attempt=attempt + 1, fact_count=len(extraction.facts))
                break
            except ValueError as error:
                last_error = str(error)
                diagnostic("selection_validation_failed", attempt=attempt + 1, error=last_error)
        else:
            raise RuntimeError(f"source selection failed one-call contract validation: {last_error}")
    except Exception as error:
        diagnostic("runner_failed", error_type=type(error).__name__, error=str(error))
        write_failure(error, len(usages), last_error)
        raise
    payload = {
        "selection": extraction.model_dump(mode="json"),
        "selection_contract": args.selection_contract,
        "materialized_evidence": [
            {
                "fact_id": row.fact_id,
                "field_name": row.field_name,
                "status": row.status,
                "semantic_role": row.semantic_role,
                "subject_role": row.subject_role,
                "source_blocks": row.source_blocks,
                "value_source": row.value_source.model_dump(mode="json") if row.value_source else None,
                "context_blocks": row.context_blocks,
                "organization_names": row.organization_names,
                "organization_sources": [source.model_dump(mode="json") for source in row.organization_sources],
                "role_raw": row.role_raw,
                "role_source_block_id": row.role_source_block_id,
                "role_source": row.role_source.model_dump(mode="json") if row.role_source else None,
                "canonical_role": row.canonical_role,
                "primary_component_id": row.primary_component_id,
                "applicability_component_ids": row.applicability_component_ids,
                "modifies_fact_ids": row.modifies_fact_ids,
                "recipient_fact_ids": row.recipient_fact_ids,
                "basis_fact_ids": row.basis_fact_ids,
            }
            for row in evidence
        ],
        "materialized_components": [
            {
                "support_component_id": row.support_component_id,
                "name_raw": row.name_raw,
                "name_source_block_id": row.name_source_block_id,
            }
            for row in components
        ],
        "support_facets": [facet.model_dump(mode="json") for facet in extraction.support_facets],
        "support_scale_measures": [
            measure.model_dump(mode="json") for measure in derived_scale_measures
        ],
        "numeric_candidates": [candidate.model_dump(mode="json") for candidate in numeric_candidates],
        # v0.2 offset validation is defined against the exact CandidatePack
        # strings, including its table-cell projection convention.
        "source_block_texts": {block.block_id: block.text for block in pack.blocks},
        "usage": {
            # Folds in every correction call's usage alongside the first
            # selection LLM's own attempts, so total token accounting covers
            # the whole runner execution, not just its one-call/repair loop.
            "input_tokens": sum(usage.input_tokens for usage in usages)
                + sum(usage.input_tokens for usage in correction_usages),
            "output_tokens": sum(usage.output_tokens for usage in usages)
                + sum(usage.output_tokens for usage in correction_usages),
            "total_tokens": sum(usage.total_tokens for usage in usages)
                + sum(usage.total_tokens for usage in correction_usages),
            "cached_input_tokens": sum(usage.input_tokens_details.cached_tokens for usage in usages)
                + sum(usage.input_tokens_details.cached_tokens for usage in correction_usages),
        },
        "attempt_count": len(usages),
        # Real correction-model calls only -- a memoized cache hit for a
        # repeated deterministic resolution is not counted again here.
        "correction_call_count": len(correction_usages),
        # Audit metadata for facts whose value_anchor needed correction:
        # fact_id, source_block_id, candidate_count, and the final resolved
        # value_source only -- never a candidate_id.
        "corrected_anchor_audit": build_corrected_anchor_audit(evidence, correction_audit),
        "input_block_count": len(pack.blocks),
        "input_table_metrics": pack_metrics,
        "server_component_normalizations": component_normalizations,
        "server_condition_variant_normalizations": condition_variant_normalizations,
        "server_preserved_fact_normalizations": preserved_fact_normalizations,
        "server_preservation_fallback": preservation_fallback,
        "common_ir_identity": common_ir_v1_identity(document) if args.common_ir else None,
        # Persist the CandidatePack text basis separately from Common IR node
        # provenance.  Final profiles use this lineage to make value_source
        # offsets reproducible without conflating CandidatePack-local block
        # IDs with original Common IR block IDs.
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": (common_ir_v1_identity(document) or {}).get("source_sha256"),
            "text_basis": "common_ir_v1_candidate_pack",
        } if args.common_ir else None,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostic("artifact_write_started", fact_count=len(extraction.facts))
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    diagnostic("artifact_write_completed", artifact_path=str(artifact_path))
    print(json.dumps({"fact_count": len(extraction.facts), "usage": payload["usage"], "artifact": str(artifact_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
