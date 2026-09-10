"""Classify attached-document sections without extracting any business facts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.notice_preparation import classify_attachment_scopes, prepare_notice, section_scope_payload
from semantic_structuring.common_ir_v1 import (
    apply_common_ir_v1_section_scopes,
    classify_common_ir_v1_attachment_scopes,
    common_ir_v1_section_scope_payload,
    common_ir_v1_identity,
    prepare_common_ir_v1,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", default="PBLN_000000000125608")
    parser.add_argument("--common-ir", type=Path, help="production Common IR v1 input; HWP/HWPX/PDF stay independent")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")

    if args.common_ir:
        document = json.loads(args.common_ir.read_text())
        prepared_unscoped, projection = prepare_common_ir_v1(document)
        if projection.notice_id != args.notice_id and args.notice_id != "PBLN_000000000125608":
            raise ValueError("--notice-id does not match Common IR")
        decisions, usage = classify_common_ir_v1_attachment_scopes(OpenAI(), document)
        prepared = apply_common_ir_v1_section_scopes(document, decisions)
        payload = common_ir_v1_section_scope_payload(prepared, identity=common_ir_v1_identity(document))
        payload["common_ir_path"] = str(args.common_ir)
        payload["source_kind"] = projection.source_kind
    else:
        ir_path = Path(f"exploratory_study/results/schema_validation/hangul_ir/{args.notice_id}.json")
        document = json.loads(ir_path.read_text())
        decisions, usage = classify_attachment_scopes(OpenAI(), document)
        prepared = prepare_notice(document, decisions)
        payload = section_scope_payload(prepared)
    payload["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
    }
    artifact_path = args.output or Path(
        f"semantic_structuring/results/common_ir_v1/{payload['notice_id']}_{payload.get('source_kind', 'legacy')}_section_scopes.json"
        if args.common_ir else f"semantic_structuring/results/{args.notice_id}_section_scopes_latest.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({**payload, "artifact": str(artifact_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
