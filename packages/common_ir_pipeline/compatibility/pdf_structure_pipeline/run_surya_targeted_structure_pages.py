#!/usr/bin/env python3
"""Run high-accuracy HTML and TableRec only on all-page structure candidates.

This preserves raw model output.  It deliberately does not decide table cells
or diagram relations.
"""
from __future__ import annotations
import argparse, dataclasses, json, os, subprocess, time
from pathlib import Path
from PIL import Image

def dump(value):
    if hasattr(value, "model_dump"): return value.model_dump()
    if dataclasses.is_dataclass(value): return dump(dataclasses.asdict(value))
    if isinstance(value, list): return [dump(x) for x in value]
    if isinstance(value, dict): return {key: dump(item) for key,item in value.items()}
    return value

def main():
    p=argparse.ArgumentParser(); p.add_argument("--surya-all-pages",type=Path,required=True); p.add_argument("--image-dir",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    source=json.loads(a.surya_all_pages.read_text(encoding="utf-8")); candidates=[page["page"] for page in source["pages"] if any(block["label"] in {"Table","Diagram"} for block in page["blocks"])]
    import surya.recognition as recognition
    from surya.inference import SuryaInferenceManager
    from surya.layout import LayoutPredictor
    from surya.recognition import RecognitionPredictor, _crop_block
    from surya.table_rec import TableRecPredictor
    recognition.SKIP_CANON_LABELS.discard("Diagram")
    started=time.perf_counter(); manager=SuryaInferenceManager(); layout_predictor=LayoutPredictor(manager); recognition_predictor=RecognitionPredictor(manager); table_predictor=TableRecPredictor(manager); pages=[]
    for page_number in candidates:
        image=Image.open(a.image_dir/f"page_{page_number:04d}.png").convert("RGB"); page_started=time.perf_counter()
        high_started=time.perf_counter(); high=dump(recognition_predictor([image],full_page=True)[0]); high_seconds=time.perf_counter()-high_started
        layout_started=time.perf_counter(); layout=layout_predictor([image])[0]; layout_seconds=time.perf_counter()-layout_started; tables=[]
        for index, box in enumerate(block for block in layout.bboxes if block.label=="Table"):
            crop=_crop_block(image,box.polygon); cell_started=time.perf_counter(); result=dump(table_predictor([crop],mode="simple")[0]); tables.append({"index":index,"source_bbox":dump(box.bbox),"crop_size":[crop.width,crop.height],"seconds":round(time.perf_counter()-cell_started,6),"result":result})
        pages.append({"page":page_number,"image_size":{"width":image.width,"height":image.height},"high_accuracy_seconds":round(high_seconds,6),"layout_seconds":round(layout_seconds,6),"table_rec_tables":tables,"high_accuracy_result":high,"page_total_seconds":round(time.perf_counter()-page_started,6)})
        print(f"page {page_number}: tables={len(tables)}",flush=True)
    gpu=subprocess.check_output(["nvidia-smi","--query-gpu=name,driver_version","--format=csv,noheader"],text=True).strip()
    out={"status":"ok","notice_id":source["notice_id"],"model":"datalab-to/surya-ocr-2","backend":"vllm","server_url":os.environ.get("SURYA_INFERENCE_URL"),"gpu":gpu,"mode":"targeted_high_accuracy_html_plus_tablerec","diagram_skip_runtime_override":True,"candidate_pages":candidates,"total_seconds":round(time.perf_counter()-started,6),"pages":pages,"preservation_note":"Raw high-accuracy HTML and TableRec cell coordinates only. No OCR/native replacement, cell promotion, or diagram relation is inferred."}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__": main()
