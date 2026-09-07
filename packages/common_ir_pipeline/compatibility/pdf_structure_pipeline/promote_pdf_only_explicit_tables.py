#!/usr/bin/env python3
"""Promote only cell-level-native-covered PDF-only tables into v0.3 IR.

Each promoted Common IR cell corresponds to one *logical* HTML cell (see
table_html_grid.py / check_pdf_only_explicit_cells.py), so row_span/col_span
reflect the table's real rowspan/colspan instead of being hardcoded to 1x1,
and a merged cell's bbox is the union of the TableRec physical cells it owns.
"""
import argparse, json
from pathlib import Path


def iou(a, b):
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    z = max(0, x1 - x0) * max(0, y1 - y0)
    area = lambda q: max(0, q[2] - q[0]) * max(0, q[3] - q[1])
    return z / (area(a) + area(b) - z) if area(a) + area(b) - z else 0


def union_bbox(boxes):
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    return x0, y0, x1, y1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-root', type=Path, required=True)
    a = p.parse_args()
    root = a.run_root
    coverage = json.loads((root / 'table_cell_native_coverage.json').read_text())['tables']
    by_notice = {}
    for x in coverage:
        if x['status'] == 'EXPLICIT':
            by_notice.setdefault(x['notice_id'], []).append(x)
    out = root / 'common_ir_v0_3_pdf_enriched'
    out.mkdir(exist_ok=True)
    summary = []
    for source in (root / 'common_ir_v0_3_pdf').glob('*.json'):
        doc = json.loads(source.read_text())
        notice = 'PBLN_' + doc['document']['document_id'].split('PBLN_')[-1]
        targets = json.loads((root / 'surya_targeted' / f'{notice}_targeted_structure.json').read_text())
        render = json.loads((root / 'rendered' / notice / 'render_manifest.json').read_text())
        scale = render['render_scale']
        heights = {x['page']: x['height'] for x in render['pages']}
        done = 0
        skipped_inconsistent = 0
        for entry in by_notice.get(notice, []):
            pg = next(x for x in targets['pages'] if x['page'] == entry['page'])
            rec = pg['table_rec_tables'][entry['table_index']]
            physical_by_id = {c['cell_id']: c for c in rec['result']['cells']}
            # Every logical cell must resolve to at least one TableRec
            # physical cell to get a bbox; coverage.py only marks a table
            # EXPLICIT when this holds, but stay defensive rather than emit
            # a table with a missing/guessed cell bbox.
            if any(not cell['physical_cell_ids'] for cell in entry['cells']):
                skipped_inconsistent += 1
                continue
            rb = rec['source_bbox']
            page = entry['page']
            pdf_bbox = [rb[0] / scale, (heights[page] - rb[3]) / scale, rb[2] / scale, (heights[page] - rb[1]) / scale]
            block = max(
                (b for b in doc['blocks'] if b.get('source_block_label') == 'Table' and b['provenance']['page'] == page),
                key=lambda b: iou(b['provenance']['bbox'], pdf_bbox),
            )
            layout = next(o['occurrence_id'] for o in block['occurrences'] if o['role'] == 'layout_region')
            ocr = next(o['occurrence_id'] for o in block['occurrences'] if o['role'] == 'ocr_text')
            block['kind'] = 'table'
            block['structure_status'] = 'explicit'
            cells = []
            for cell in entry['cells']:
                boxes = [physical_by_id[pid]['bbox'] for pid in cell['physical_cell_ids']]
                x0, y0, x1, y1 = union_bbox(boxes)
                bbox = [(rb[0] + x0) / scale, (heights[page] - (rb[1] + y1)) / scale, (rb[0] + x1) / scale, (heights[page] - (rb[1] + y0)) / scale]
                ids = [f"occ:inspector:p{page}:t{i}" for i in cell['native_text_item_indices']]
                cells.append({
                    'cell_id': f"{block['block_id']}:cell:{cell['cell_id']}",
                    'row_index': cell['row'], 'col_index': cell['column'],
                    'row_span': cell['row_span'], 'col_span': cell['col_span'],
                    'evidence_ids': [layout, ocr], 'text_occurrence_ids': ids,
                    'provenance': {
                        'method': 'surya_tablerec_native_bbox_alignment_logical_cell',
                        'page': page, 'bbox': bbox, 'coordinate_space': 'pdf_user_space',
                        'source_location': f"table_cell_native_coverage.json:{notice}:p{page}:table{entry['table_index']}:cell{cell['cell_id']}",
                    },
                })
            block['cells'] = cells
            done += 1
        (out / source.name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
        summary.append({'notice_id': notice, 'explicit_tables': done, 'skipped_inconsistent': skipped_inconsistent})
    (root / 'PDF_ONLY_EXPLICIT_TABLE_PROMOTION.json').write_text(json.dumps({'tables': summary}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
