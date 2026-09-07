#!/usr/bin/env python3
"""Assemble PDF base IR from pdf-inspector and all-page Surya raw.

No Gold input is accepted. The render manifest is the coordinate contract;
optional Surya image dimensions are diagnostics only.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path

ROOT = Path("exploratory_study/results/runpod_hybrid_ir_validation")

def pdf_bbox(bbox, scale, height):
    x0, y0, x1, y1 = bbox
    return [x0 / scale, (height-y1) / scale, x1 / scale, (height-y0) / scale]

def manifest_page_dimensions(render: dict, surya: dict):
    render_pages = {entry["page"]: entry for entry in render["pages"]}; heights, diagnostics = {}, []
    for page_data in surya["pages"]:
        page = page_data["page"]
        if page not in render_pages: raise SystemExit(f"render manifest is missing Surya page {page}")
        expected = render_pages[page]; heights[page] = expected["height"]; image_size = page_data.get("image_size")
        diagnostic = {"page":page, "render_manifest_dimensions":[expected["width"], expected["height"]], "surya_image_size_present":bool(image_size)}
        if image_size and "width" in image_size and "height" in image_size:
            actual = [image_size["width"], image_size["height"]]
            diagnostic.update({"surya_image_dimensions":actual, "surya_image_size_matches_manifest":actual == diagnostic["render_manifest_dimensions"]})
        diagnostics.append(diagnostic)
    return heights, diagnostics

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--notice-id", required=True); parser.add_argument("--native", type=Path, required=True); parser.add_argument("--render-manifest", type=Path, required=True); parser.add_argument("--surya-all-pages", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--direct-diagram-config", type=Path); args = parser.parse_args()
    native = json.loads(args.native.read_text(encoding="utf-8")); render = json.loads(args.render_manifest.read_text(encoding="utf-8")); surya = json.loads(args.surya_all_pages.read_text(encoding="utf-8"))
    if not isinstance(surya.get("pages"), list): raise SystemExit("Surya raw must provide an all-page 'pages' list")
    scale = render["render_scale"]; heights, coordinate_diagnostics = manifest_page_dimensions(render, surya); blocks = []
    for index, item in enumerate(native["text_items"]):
        blocks.append({"id":f"occ:inspector:p{item['page']}:t{index}", "kind":"paragraph", "granularity":"native_text_occurrence", "text":item["text"], "structure_status":"explicit", "provenance":{"page":item["page"], "source_location":f"text_items[{index}]", "bbox":[item["x"],item["y"],item["x"]+item["width"],item["y"]+item["height"]], "coordinate_space":"pdf_user_space", "extraction_method":"pdf_inspector"}})
    page_meta=[]; required_metrics=("page","layout_seconds","layout_block_ocr_seconds","page_total_seconds","block_count","diagram_block_count")
    for page_position, page_data in enumerate(surya["pages"]):
        page=page_data["page"]; missing=[key for key in required_metrics if key not in page_data]
        if missing: raise SystemExit(f"Surya page {page} is missing required metrics: {missing}")
        page_meta.append({key:page_data[key] for key in required_metrics})
        for raw_block in page_data["blocks"]:
            label=raw_block["label"]; kind,status=("paragraph","explicit")
            if label in ("Table","Form"): kind,status="table_candidate","partial"
            elif label=="Diagram": kind,status="diagram_candidate","partial"
            # Some all-page runners expose only page-level OCR time.  Keep it
            # unavailable rather than inventing a per-block allocation.
            ocr_metadata={"block_ocr_seconds":raw_block.get("block_ocr_seconds"), "block_ocr_seconds_status":"reported" if "block_ocr_seconds" in raw_block else "unavailable_page_level_only", "block_image_size":raw_block.get("block_image_size")}
            blocks.append({"id":f"occ:surya:p{page}:b{raw_block['block_index']}", "kind":kind, "layout_label":label, "text":raw_block["result"].get("raw"), "structure_status":status, "provenance":{"page":page,"source_location":f"{args.surya_all_pages}:pages[{page_position}].blocks[{raw_block['block_index']}]","bbox":pdf_bbox(raw_block["source_bbox"],scale,heights[page]),"coordinate_space":"pdf_user_space","render_bbox":raw_block["source_bbox"],"extraction_method":"surya"},"ocr_metadata":ocr_metadata,"preservation_note":"OCR/layout occurrence; it never replaces pdf-inspector native text."})
    relations=[]; diagram_sidecars=[]
    if args.direct_diagram_config:
        config=json.loads(args.direct_diagram_config.read_text(encoding="utf-8")); evidence=json.loads((ROOT/config["diagram_evidence"]["source_path"]).read_text(encoding="utf-8")); diagram=evidence["result"]["blocks"][config["diagram_evidence"]["diagram_block_index"]]; page=config["diagram_evidence"]["page"]
        if diagram["label"]!="Diagram" or not diagram.get("html"): raise SystemExit("configured Diagram evidence is missing")
        evidence_id=f"occ:surya:p{page}:diagram_high:0"; diagram_bbox=pdf_bbox(diagram["bbox"],scale,heights[page]); blocks.append({"id":evidence_id,"kind":"diagram_candidate","layout_label":"Diagram","text":diagram["html"],"structure_status":"explicit","provenance":{"page":page,"source_location":f"{config['diagram_evidence']['source_path']}:result.blocks[{config['diagram_evidence']['diagram_block_index']}]","bbox":diagram_bbox,"coordinate_space":"pdf_user_space","render_bbox":diagram["bbox"],"extraction_method":"surya"},"preservation_note":"Direct Diagram evidence. Arrow symbols are retained as observed OCR output."})
        node_ids={}
        for node in config["nodes"]:
            item=native["text_items"][node["native_text_item_index"]]; bbox=[item["x"],item["y"],item["x"]+item["width"],item["y"]+item["height"]]; inside=bbox[0]>=diagram_bbox[0] and bbox[1]>=diagram_bbox[1] and bbox[2]<=diagram_bbox[2] and bbox[3]<=diagram_bbox[3]
            if item["page"]!=page or item["text"]!=node["expected_text"] or not inside or node["expected_text"] not in diagram["html"]: raise SystemExit(f"native Diagram node validation failed: {node['id']}")
            node_ids[node["id"]]=f"occ:inspector:p{item['page']}:t{node['native_text_item_index']}"
        for index, edge in enumerate(config["edges"],1):
            if edge["arrow"] not in diagram["html"]: raise SystemExit(f"observed arrow missing from Diagram HTML: {edge['arrow']}")
            relations.append({"relation_id":f"rel:diagram:p{page}:{index}","kind":"diagram_edge","from_id":node_ids[edge["from"]],"to_id":node_ids[edge["to"]],"inferred":False,"structure_status":"explicit","evidence_ids":[evidence_id],"provenance":{"page":page,"source_location":str(args.direct_diagram_config),"bbox":None,"coordinate_space":None,"extraction_method":"manual_curation_from_surya_html"},"observed_arrow":edge["arrow"]})
        diagram_sidecars.append({"kind":"direct_diagram_relations","artifact":str(args.direct_diagram_config),"status":"explicit"})
    output={"schema_version":"common_ir_pdf_base_v2","document":{"source_format":"pdf","source_path":native["source_path"],"source_notice_key":args.notice_id,"scope":"whole_document"},"blocks":blocks,"relations":relations,"conflicts":[],"sidecars":diagram_sidecars,"assembly":{"native_source_artifact":str(args.native),"surya_all_pages_artifact":str(args.surya_all_pages),"render_manifest":str(args.render_manifest),"coordinate_transform":"x_pdf=x_render/scale; y_pdf=(render_manifest_height-y_render)/scale","coordinate_contract":"render_manifest_page_dimensions_and_render_scale","coordinate_diagnostics":coordinate_diagnostics,"surya_page_metrics":page_meta,"native_occurrence_count":len(native["text_items"])}}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps({"output":str(args.output),"blocks":len(blocks),"relations":len(relations)},ensure_ascii=False))

if __name__=="__main__": main()
