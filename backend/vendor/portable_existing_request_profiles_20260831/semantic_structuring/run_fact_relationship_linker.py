"""Deprecated two-call relationship experiment; intentionally not executable."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.fact_relationships import (
    FactRelationshipExtraction,
    build_relationship_payload,
    validate_relationships,
)
from semantic_structuring.pipeline import _openai_schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", required=True)
    args = parser.parse_args()
    raise SystemExit(
        "Deprecated: relationships must be selected in the one source-selection call; "
        "this separate LLM linker is intentionally disabled."
    )
    source_path = Path(f"semantic_structuring/results/{args.notice_id}_source_selection_v0.3_latest.json")
    source = json.loads(source_path.read_text())
    payload = build_relationship_payload(source)
    output_path = Path(f"semantic_structuring/results/{args.notice_id}_fact_relationships_v0.1_latest.json")
    if payload is None:
        output_path.write_text(json.dumps({"notice_id": args.notice_id, "skipped": True, "reason": "no conditional relation candidates"}, ensure_ascii=False, indent=2))
        print(json.dumps({"notice_id": args.notice_id, "skipped": True}, ensure_ascii=False))
        return

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")
    instructions = (
        "Decide only the supplied candidate relationships between already selected public-notice facts. "
        "Return an edge only when the exact value blocks plus their headers/context establish it. "
        "direct_recipient means the target fact is the entity receiving payment or settlement of the source support/payment fact. "
        "eligibility_or_calculation_basis means the target fact's employment, activity, equipment, or condition determines the source support's eligibility, amount, or payment. "
        "Return no edge for mere co-occurrence in one program or table. Never write, summarize, correct, or select facts; output only fact ids and relation_type."
    )
    client = OpenAI()
    usages = []
    last_error: str | None = None
    for _ in range(2):
        suffix = f" Previous output failed: {last_error}. Correct it." if last_error else ""
        response = client.responses.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
            reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
            store=False,
            input=[{"role": "system", "content": instructions + suffix}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            text={"format": {"type": "json_schema", "name": "fact_relationships", "strict": True, "schema": _openai_schema(FactRelationshipExtraction)}},
        )
        usages.append(response.usage)
        extraction = FactRelationshipExtraction.model_validate_json(response.output_text)
        try:
            validate_relationships(extraction, payload)
            break
        except ValueError as error:
            last_error = str(error)
    else:
        raise RuntimeError(f"relationship linker failed after one retry: {last_error}")
    output_path.write_text(json.dumps({
        "notice_id": args.notice_id,
        "source_selection_artifact": str(source_path),
        "candidate_count": len(payload["candidate_relations"]),
        "relations": extraction.model_dump(mode="json")["relations"],
        "usage": {
            "input_tokens": sum(item.input_tokens for item in usages),
            "output_tokens": sum(item.output_tokens for item in usages),
            "total_tokens": sum(item.total_tokens for item in usages),
            "cached_input_tokens": sum(item.input_tokens_details.cached_tokens for item in usages),
        },
        "attempt_count": len(usages),
    }, ensure_ascii=False, indent=2))
    print(json.dumps({"notice_id": args.notice_id, "candidate_count": len(payload["candidate_relations"]), "relation_count": len(extraction.relations), "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
