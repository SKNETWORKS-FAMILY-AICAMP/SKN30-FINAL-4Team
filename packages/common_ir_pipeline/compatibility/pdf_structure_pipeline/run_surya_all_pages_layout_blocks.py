#!/usr/bin/env python3
"""All-page OCR by Surya-detected layout blocks; native/OCR remain separate."""
from __future__ import annotations
import argparse, dataclasses, json, os, subprocess, time
from pathlib import Path
from PIL import Image

def dump(value):
    if hasattr(value, "model_dump"): return value.model_dump()
    if dataclasses.is_dataclass(value): return dump(dataclasses.asdict(value))
    if hasattr(value, "__dict__"): return dump(vars(value))
    if isinstance(value, list): return [dump(item) for item in value]
    if isinstance(value, dict): return {key: dump(item) for key, item in value.items()}
    return value

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--block-max-tokens", type=int, default=2048)
    args = parser.parse_args(); images = sorted(args.image_dir.glob("page_*.png"))
    if not images: raise SystemExit(f"no rendered pages under {args.image_dir}")
    from surya.inference import SuryaInferenceManager
    from surya.inference.schema import BatchInputItem
    from surya.layout import LayoutPredictor
    from surya.recognition import _crop_block
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"], text=True).strip()
    all_started = time.perf_counter(); started = time.perf_counter(); manager = SuryaInferenceManager(); manager_seconds = time.perf_counter()-started
    layout_predictor = LayoutPredictor(manager); pages=[]
    for page, path in enumerate(images, 1):
        page_started=time.perf_counter(); image=Image.open(path).convert("RGB")
        started=time.perf_counter(); layout=layout_predictor([image])[0]; layout_seconds=time.perf_counter()-started
        boxes=list(layout.bboxes); crops=[_crop_block(image, box.polygon) for box in boxes]
        started=time.perf_counter(); results=manager.generate([BatchInputItem(image=crop,prompt_type="block",max_tokens=args.block_max_tokens) for crop in crops]); blocks_seconds=time.perf_counter()-started
        blocks=[]
        for index,(box,crop,result) in enumerate(zip(boxes,crops,results)):
            blocks.append({"block_index":index,"label":box.label,"source_polygon":dump(box.polygon),"source_bbox":dump(box.bbox),"block_image_size":{"width":crop.width,"height":crop.height},"result":dump(result)})
        pages.append({"page":page,"image_path":str(path),"image_size":{"width":image.width,"height":image.height},"layout_seconds":round(layout_seconds,6),"layout_block_ocr_seconds":round(blocks_seconds,6),"block_count":len(blocks),"diagram_block_count":sum(1 for block in blocks if block["label"]=="Diagram"),"blocks":blocks,"page_total_seconds":round(time.perf_counter()-page_started,6),"layout":dump(layout)})
    payload={"status":"ok","notice_id":args.notice_id,"model":"datalab-to/surya-ocr-2","backend":"vllm","server_url":os.environ.get("SURYA_INFERENCE_URL"),"gpu":gpu,"mode":"all_page_layout_plus_all_detected_layout_block_ocr","manager_seconds":round(manager_seconds,6),"total_seconds":round(time.perf_counter()-all_started,6),"pages":pages,"preservation_note":"Every Surya-detected layout block on every rendered page received an OCR occurrence. No native/OCR winner, table cell, or diagram relation is inferred."}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

if __name__=="__main__": main()
