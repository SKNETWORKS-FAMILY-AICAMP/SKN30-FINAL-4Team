"""Legacy semantic-structuring pipeline; production source selection is one call."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from openai import OpenAI
from openai import APIError
from pydantic import BaseModel, ValidationError

from .models import CandidatePack, SemanticExtraction
from .validation import ValidationIssue, validate_extraction


@dataclass(frozen=True)
class CallUsage:
    attempt: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None


@dataclass
class PipelineResult:
    status: str  # passed | unresolved
    extraction: BaseModel | None
    issues: list[ValidationIssue] = field(default_factory=list)
    usage: list[CallUsage] = field(default_factory=list)

    @property
    def comparison_facts(self) -> list:
        if self.status != "passed" or not self.extraction:
            return []
        return [fact for fact in self.extraction.facts if fact.status.value in {"identified", "partially_identified"}]

    @property
    def search_only_facts(self) -> list:
        if self.status != "passed" or not self.extraction:
            return []
        return [fact for fact in self.extraction.facts if fact.status.value not in {"identified", "partially_identified"}]


def _openai_schema(response_model: type[BaseModel] = SemanticExtraction) -> dict:
    schema = response_model.model_json_schema()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def _usage(response: object, attempt: int) -> CallUsage:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "input_tokens_details", None)
    return CallUsage(
        attempt=attempt,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        cached_input_tokens=getattr(details, "cached_tokens", None),
    )


def _instructions(issues: list[ValidationIssue] | None = None) -> str:
    base = (
        "Extract only facts supported by the supplied common-IR candidate pack. "
        "Do not infer image or table relations. Copy value_raw verbatim from cited source text: do not shorten, add ellipses, or paraphrase it. "
        "model_summary is optional convenience wording only and is never source evidence. "
        "Split independent source claims into separate facts. Use purpose_goal for the business goal; "
        "target for applicant, beneficiary, or condition subject; support_scale for money, counts, budgets, or periods; "
        "cost_sharing for beneficiary-borne costs; and delivery_role for named institutions and their roles. "
        "Use table_catalog for searchable table structures and do not turn application forms or template text into current-notice facts. "
        "Selection preference or bonus information belongs only in B rule table_catalog, never in A facts. "
        "For first-pass facts, omit a standalone number, budget, or period when its business subject is absent from the cited text "
        "and another block gives a more specific conflicting or duplicate figure; preserve that source only in the IR. "
        "Create participation_type only for two or more explicit alternative types a participant can select. "
        "Partner, demand, operating, and cooperating organizations are notice-scoped delivery_roles, never support components."
    )
    if not issues:
        return base
    errors = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
    return f"{base} Your previous result failed these deterministic checks: {errors}. Return a corrected complete result."


def _model_input(pack: CandidatePack) -> str:
    """Send source evidence only; the human test label must not steer extraction."""

    return json.dumps(pack.model_dump(exclude={"question"}), ensure_ascii=False)


def run_candidate_pack(
    client: OpenAI,
    pack: CandidatePack,
    *,
    response_model: type[BaseModel] = SemanticExtraction,
    max_retries: int = 0,
) -> PipelineResult:
    """Call Structured Outputs once and surface deterministic validation failure."""

    if max_retries != 0:
        raise ValueError("current policy permits no automatic retry")

    schema = _openai_schema(response_model)
    usage: list[CallUsage] = []
    issues: list[ValidationIssue] = []
    last_extraction: BaseModel | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.responses.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"), reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")}, store=False,
                input=[{"role": "system", "content": _instructions(issues or None)}, {"role": "user", "content": _model_input(pack)}],
                text={"format": {"type": "json_schema", "name": "semantic_extraction", "strict": True, "schema": schema}},
            )
            usage.append(_usage(response, attempt))
            if response.status != "completed" or not response.output_text:
                issues = [ValidationIssue("incomplete_response", f"response status: {response.status}")]
                continue
            extraction = response_model.model_validate_json(response.output_text)
            last_extraction = extraction
            issues = validate_extraction(extraction, pack)
        except (APIError, ValidationError, ValueError) as error:
            issues = [ValidationIssue("schema_or_request_failure", str(error))]
            continue
        if not issues:
            return PipelineResult(status="passed", extraction=extraction, usage=usage)

    return PipelineResult(status="unresolved", extraction=last_extraction, issues=issues, usage=usage)


def result_json(result: PipelineResult) -> str:
    """Safe summary: contains no environment values or credentials."""

    return json.dumps(
        {
            "status": result.status,
            "issues": [asdict(issue) for issue in result.issues],
            "usage": [asdict(item) for item in result.usage],
            "comparison_fact_count": len(result.comparison_facts),
            "search_only_fact_count": len(result.search_only_facts),
            "result": result.extraction.model_dump(mode="json") if result.extraction else None,
        },
        ensure_ascii=False,
        indent=2,
    )
