#!/usr/bin/env python3
"""Evidence-only gate: high-accuracy HTML + TableRec + native bbox coverage.

Structure agreement is judged on the *logical* grid size, i.e. HTML rows/cols
after resolving rowspan/colspan the way a browser would (see
table_html_grid.py) — not a raw <tr> count, which silently disagrees with
TableRec's physical grid whenever any cell spans more than one row.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from table_html_grid import parse_html_table_grid


def iou(a, b):
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area = lambda z: max(0, z[2] - z[0]) * max(0, z[3] - z[1])
    return inter / (area(a) + area(b) - inter) if area(a) + area(b) - inter else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--targeted-dir', type=Path, required=True)
    p.add_argument('--run-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    tables = []
    for path in sorted(a.targeted_dir.glob('*_targeted_structure.json')):
        t = json.loads(path.read_text())
        n = t['notice_id']
        native = json.loads((a.run_root / 'raw/pdf_inspector' / f'{n}.json').read_text())
        render = json.loads((a.run_root / 'rendered' / n / 'render_manifest.json').read_text())
        pages = {x['page']: x for x in render['pages']}
        scale = render['render_scale']
        for pg in t['pages']:
            hs = [b for b in pg['high_accuracy_result']['blocks'] if b['label'] == 'Table']
            for ix, rec in enumerate(pg['table_rec_tables']):
                h = max(hs, key=lambda b: iou(b['bbox'], rec['source_bbox']), default=None)
                overlap = iou(h['bbox'], rec['source_bbox']) if h else 0
                logical = parse_html_table_grid(h['html']) if h else {'rows': 0, 'cols': 0}
                hr, hc = logical['rows'], logical['cols']
                rr = len(rec['result'].get('rows', []))
                rc = len(rec['result'].get('cols', []))
                x0, y0, x1, y1 = rec['source_bbox']
                height = pages[pg['page']]['height']
                count = 0
                for item in native['text_items']:
                    if item['page'] != pg['page']:
                        continue
                    cx = (item['x'] + item['width'] / 2) * scale
                    cy = height - (item['y'] + item['height'] / 2) * scale
                    if x0 <= cx <= x1 and y0 <= cy <= y1:
                        count += 1
                exact = overlap >= .95 and (hr, hc) == (rr, rc) and count > 0
                reasons = []
                if overlap < .95:
                    reasons.append('bbox_not_matched')
                if (hr, hc) != (rr, rc):
                    reasons.append('html_grid_differs_from_tablerec')
                if not count:
                    reasons.append('no_native_occurrence_in_table')
                tables.append({
                    'notice_id': n, 'page': pg['page'], 'table_index': ix,
                    'bbox_iou': round(overlap, 4),
                    'html_grid': [hr, hc], 'tablerec_grid': [rr, rc],
                    'tablerec_cell_count': len(rec['result'].get('cells', [])),
                    'native_occurrences_with_center_inside': count,
                    'structure_gate': 'EXPLICIT_CANDIDATE' if exact else 'PARTIAL',
                    'failure_reasons': reasons,
                })
    a.output.write_text(json.dumps({'scope': 'PDF-only blind; no Gold input', 'tables': tables}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'tables': len(tables), 'explicit_candidates': sum(x['structure_gate'] == 'EXPLICIT_CANDIDATE' for x in tables)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
