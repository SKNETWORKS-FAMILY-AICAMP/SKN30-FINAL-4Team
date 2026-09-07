#!/usr/bin/env python3
"""Create all-in-one Common IR v0.3 from v0.2 adapter outputs."""
from __future__ import annotations
import argparse, copy, json
from pathlib import Path
from jsonschema import Draft202012Validator
from common_ir_v0_2_schema import schema as v02_schema, validation_errors as v02_errors

CANDIDATE_SOURCES = {
 "124743": {"block_id":"occ:surya:p9:b2","path":"diagram_candidates/PBLN_000000000124743_p9_llm_grid_candidate.json"},
 "125098": {"block_id":"occ:surya:p3:b4","path":"diagram_candidates/PBLN_000000000125098_p3_llm_candidate_v2.json"},
 "125611": {"block_id":"occ:surya:p13:b1","path":"diagram_candidates/PBLN_000000000125611_p13_llm_candidate.json"},
}

def schema():
 s=copy.deepcopy(v02_schema())
 s["$id"]="common_ir_v0_3.schema.json";s["title"]="공통 IR v0.3";s["properties"]["schema_version"]["const"]="common_ir_v0_3"
 candidate={"type":"object","additionalProperties":False,"required":["candidate_id","kind","status","method","input_scope","nodes","edges","uncertainties","provenance"],
  "properties":{"candidate_id":{"type":"string","minLength":1},"kind":{"const":"diagram_relation_candidate"},"status":{"const":"candidate"},
  "method":{"type":"string","minLength":1},"input_scope":{"type":"string","minLength":1},
  "nodes":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["node_id","label","source_phrase"],
    "properties":{"node_id":{"type":"string","minLength":1},"label":{"type":"string","minLength":1},"source_phrase":{"type":"string","minLength":1}}}},
  "edges":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["from_node_id","to_node_id","evidence_phrase"],
    "properties":{"from_node_id":{"type":"string","minLength":1},"to_node_id":{"type":"string","minLength":1},"evidence_phrase":{"type":"string","minLength":1}}}},
  "uncertainties":{"type":"array","items":{"type":"string"}},
  "provenance":{"$ref":"#/$defs/provenance"}}}
 s["$defs"]["block"]["properties"]["candidate_relations"]={"type":"array","items":candidate}
 return s

def errors(doc):
 out=[x.message for x in Draft202012Validator(schema()).iter_errors(doc)]
 check=copy.deepcopy(doc);check["schema_version"]="common_ir_v0_2"
 for block in check["blocks"]: block.pop("candidate_relations",None)
 out.extend(v02_errors(check))
 return out

def notice_from_name(path):
 return path.name.split("_")[1][-6:]

def embed_candidate(doc, root, notice, spec):
 raw=json.loads((root/spec["path"]).read_text())
 result=raw["response"]["output"];source=raw["source"]
 block=next(b for b in doc["blocks"] if b["block_id"]==spec["block_id"])
 nodes=[];by_label={}
 for i,node in enumerate(result["nodes"],1):
  node_id=f"{block['block_id']}:candidate:n{i}";by_label[node["label"]]=node_id
  nodes.append({"node_id":node_id,"label":node["label"],"source_phrase":node["source_phrase"]})
 edges=[]
 for edge in result["edges"]:
  if edge["from_label"] not in by_label or edge["to_label"] not in by_label: raise ValueError("candidate edge label absent from nodes")
  edges.append({"from_node_id":by_label[edge["from_label"]],"to_node_id":by_label[edge["to_label"]],"evidence_phrase":edge["evidence_phrase"]})
 block["candidate_relations"]=[{"candidate_id":f"{block['block_id']}:candidate:1","kind":"diagram_relation_candidate","status":"candidate",
  "method":"llm_from_surya_diagram_only","input_scope":raw["input_scope"],"nodes":nodes,"edges":edges,
  "uncertainties":result["uncertainties"],
  "provenance":{"method":"openai_from_surya_diagram_only","page":source["page"],"bbox":source.get("bbox_render"),"coordinate_space":"rendered_page_px","source_location":spec["path"]}}]
 if spec["path"] not in doc["document"]["raw_artifact_ids"]: doc["document"]["raw_artifact_ids"].append(spec["path"])

def transform(source, root, notice):
 doc=json.loads(source.read_text());doc["schema_version"]="common_ir_v0_3"
 if doc["document"]["source_kind"] == "pdf" and notice in CANDIDATE_SOURCES: embed_candidate(doc,root,notice,CANDIDATE_SOURCES[notice])
 doc["document"]["raw_artifact_ids"]=list(dict.fromkeys(doc["document"]["raw_artifact_ids"]))
 return doc

def main():
 p=argparse.ArgumentParser()
 p.add_argument("--pdf-v0-2-dir",type=Path,required=True);p.add_argument("--rhwp-v0-2-dir",type=Path,required=True)
 p.add_argument("--run-root",type=Path,required=True);p.add_argument("--pdf-output-dir",type=Path,required=True)
 p.add_argument("--rhwp-output-dir",type=Path,required=True);p.add_argument("--schema-output",type=Path,required=True)
 a=p.parse_args();a.pdf_output_dir.mkdir(parents=True,exist_ok=True);a.rhwp_output_dir.mkdir(parents=True,exist_ok=True)
 a.schema_output.write_text(json.dumps(schema(),ensure_ascii=False,indent=2)+"\n")
 outputs=[]
 for source in sorted(a.pdf_v0_2_dir.glob("*.json")):
  notice=notice_from_name(source);doc=transform(source,a.run_root,notice);err=errors(doc)
  if err: raise SystemExit(source.name+": "+"\n".join(err))
  out=a.pdf_output_dir/source.name;out.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n");outputs.append(str(out))
 for source in sorted(a.rhwp_v0_2_dir.glob("*.json")):
  notice=notice_from_name(source);doc=transform(source,a.run_root,notice);err=errors(doc)
  if err: raise SystemExit(source.name+": "+"\n".join(err))
  out=a.rhwp_output_dir/source.name;out.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n");outputs.append(str(out))
 print(json.dumps({"outputs":len(outputs),"schema":str(a.schema_output),"validation_errors":0},ensure_ascii=False))
if __name__=="__main__":main()



