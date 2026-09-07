"""Legacy document-level semantic-extraction experiment.

It deliberately flattens all tables and must never be used for production A
profile extraction.  An explicit flag is required to run it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.evaluation import evaluate_extraction
from semantic_structuring.models import CandidatePack, FlatSemanticExtraction, SourceBlock, SourceRelation
from semantic_structuring.pipeline import result_json, run_candidate_pack


def _table_text(table: dict) -> str:
    cells = []
    for cell in table.get("cells", []):
        text = " ".join(block.get("text", "") for block in cell.get("blocks", [])).strip()
        if text:
            cells.append(f"r{cell['row']}c{cell['col']}: {text}")
    return f"[table {table.get('rows')}x{table.get('cols')}] " + " | ".join(cells)


def build_document_pack(ir_path: str | Path) -> CandidatePack:
    document = json.loads(Path(ir_path).read_text())
    blocks = []
    for index, item in enumerate(document["ir"]["body"]):
        text = item.get("text", "") if item["kind"] == "paragraph" else _table_text(item)
        if text.strip():
            blocks.append(
                SourceBlock(
                    block_id=f"body[{index}]",
                    text=text,
                    relation=SourceRelation.CANDIDATE,
                    block_kind=item["kind"],
                )
            )
    return CandidatePack(
        pack_id=f"{document['notice_id']}-document-v0.1",
        notice_id=document["notice_id"],
        extraction_scope="document",
        question="Document-level evaluation label only; never sent to the model.",
        blocks=blocks,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-legacy-experiment", action="store_true")
    args = parser.parse_args()
    if not args.allow_legacy_experiment:
        raise SystemExit("Legacy experiment disabled. Pass --allow-legacy-experiment only for isolated historical evaluation.")
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")

    ir_path = "exploratory_study/results/schema_validation/hangul_ir/PBLN_000000000125002.json"
    gold_path = "semantic_structuring/golden/PBLN_000000000125002.v0.1.json"
    result = run_candidate_pack(OpenAI(), build_document_pack(ir_path), response_model=FlatSemanticExtraction)
    payload = json.loads(result_json(result))
    if result.extraction is not None:
        payload["evaluation"] = evaluate_extraction(result.extraction, gold_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
