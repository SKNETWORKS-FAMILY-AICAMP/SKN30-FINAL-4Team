#!/usr/bin/env python3
"""Promote an evidence-enriched PDF Common IR to v0.2 and add continuation."""
import argparse
import json
from pathlib import Path
from common_ir_v0_2_schema import validation_errors


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--input',type=Path,required=True); parser.add_argument('--continuation',type=Path); parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    doc=json.loads(args.input.read_text()); doc['schema_version']='common_ir_v0_2'
    if args.continuation:
        evidence=json.loads(args.continuation.read_text())
        relation={'relation_id':f"{evidence['from_table_occurrence_id']}--continues--{evidence['to_table_occurrence_id']}",'kind':'table_continuation','from_id':evidence['from_table_occurrence_id'],'to_id':evidence['to_table_occurrence_id'],'evidence_ids':[pair['p2'] for pair in evidence['evidence']['repeated_native_headers']]+[pair['p3'] for pair in evidence['evidence']['repeated_native_headers']],'inferred':False,'provenance':{'method':'pdf_repeated_header_geometry_page_boundary','page':2,'bbox':None,'coordinate_space':None,'source_location':str(args.continuation)}}
        doc['relations'].append(relation); doc['document']['raw_artifact_ids'].append(str(args.continuation)); doc['document']['raw_artifact_ids']=list(dict.fromkeys(doc['document']['raw_artifact_ids']))
    errors=validation_errors(doc)
    if errors: raise SystemExit('\n'.join(errors))
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(args.output),'relations':len(doc['relations']),'validation_errors':0}))
if __name__=='__main__':main()
