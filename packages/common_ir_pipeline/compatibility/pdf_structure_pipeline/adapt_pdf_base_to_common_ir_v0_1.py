#!/usr/bin/env python3
"""Create standalone production common_ir_v0_1 from PDF base IR only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common_ir_v0_1_schema import validation_errors


def provenance(raw: dict, source_location: str, method: str | None = None) -> dict:
    return {"method": method or raw.get("method", raw.get("extraction_method", "pdf_hybrid")), "page": raw.get("page"), "bbox": raw.get("bbox"), "coordinate_space": raw.get("coordinate_space"), "source_location": source_location}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--pdf-base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = json.loads(args.pdf_base.read_text(encoding="utf-8"))
    id_map, ocr_id_map = {}, {}
    for source in base["blocks"]:
        if source["provenance"]["extraction_method"] == "pdf_inspector":
            id_map[source["id"]] = source["id"]
        else:
            id_map[source["id"]] = f"{source['id']}:layout"
            if source.get("text") is not None:
                ocr_id_map[source["id"]] = f"{source['id']}:ocr"

    blocks = []
    for index, source in enumerate(base["blocks"]):
        source_p = source["provenance"]
        block_p = provenance(source_p, f"{args.pdf_base}#/blocks/{index}")
        if source_p["extraction_method"] == "pdf_inspector":
            occurrences = [{"occurrence_id": source["id"], "role": "native_text", "text": source.get("text", ""), "provenance": block_p}]
            kind, status = "paragraph", "explicit"
        else:
            occurrences = [{"occurrence_id": id_map[source["id"]], "role": "layout_region", "provenance": provenance(source_p, f"{args.pdf_base}#/blocks/{index}/provenance", "surya_layout")}]
            if source.get("text") is not None:
                occurrences.append({"occurrence_id": ocr_id_map[source["id"]], "role": "ocr_text", "text": source["text"], "provenance": provenance(source_p, f"{args.pdf_base}#/blocks/{index}/text", "surya_ocr")})
            kind, status = source["kind"], source["structure_status"]
        block = {"block_id": source["id"], "kind": kind, "structure_status": status, "occurrences": occurrences, "provenance": block_p}
        if source.get("layout_label"):
            block["source_block_label"] = source["layout_label"]
        if source.get("cells"):
            block["cells"] = [{**{key: value for key, value in cell.items() if key not in {"evidence_ids", "text_occurrence_ids", "provenance"}}, "evidence_ids": [id_map[item] for item in cell["evidence_ids"]], "text_occurrence_ids": [id_map[item] for item in cell.get("text_occurrence_ids", [])], "provenance": provenance(cell["provenance"], f"{args.pdf_base}#/blocks/{index}/cells/{cell['cell_id']}")} for cell in source["cells"]]
        blocks.append(block)

    relations = []
    for index, relation in enumerate(base.get("relations", [])):
        converted = {"relation_id": relation["relation_id"], "kind": relation["kind"], "from_id": id_map[relation["from_id"]], "to_id": id_map[relation["to_id"]], "evidence_ids": [ocr_id_map.get(item, id_map[item]) for item in relation["evidence_ids"]], "inferred": False, "provenance": provenance(relation["provenance"], f"{args.pdf_base}#/relations/{index}")}
        if relation.get("observed_arrow"):
            converted["observed_arrow"] = relation["observed_arrow"]
        relations.append(converted)
    metrics = base.get("assembly", {}).get("surya_page_metrics", [])
    page_count = max((metric["page"] for metric in metrics), default=max((block["provenance"]["page"] for block in blocks if block["provenance"]["page"] is not None), default=0))
    assembly = base.get("assembly", {})
    raw_artifacts = [str(args.pdf_base)] + [assembly[key] for key in ("native_source_artifact", "surya_all_pages_artifact", "render_manifest") if assembly.get(key)]
    result = {"schema_version": "common_ir_v0_1", "document": {"document_id": f"pdf:{args.notice_id}", "source_kind": "pdf", "artifact_role": "production", "page_count": page_count, "raw_artifact_ids": list(dict.fromkeys(raw_artifacts)), "provenance": {"method": "pdf_hybrid", "page": None, "bbox": None, "coordinate_space": None, "source_location": str(args.pdf_base)}}, "blocks": blocks, "conflicts": [], "relations": relations}
    errors = validation_errors(result)
    if errors:
        raise SystemExit("Common IR validation failed:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "blocks": len(blocks), "relations": len(relations), "validation_errors": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
