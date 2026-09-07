#!/usr/bin/env python3
"""Cell-level native coverage check for TableRec grids that passed the
structure gate.

Coverage is judged per *logical* HTML cell (rowspan/colspan resolved via
table_html_grid.py), not per raw TableRec physical grid cell. TableRec always
emits one physical cell per (row_id, col_id) slot even when the HTML says a
handful of those slots are one merged cell — so a merged cell's single native
text item only ever lands inside *one* of its member physical cells. Requiring
every physical slot to individually hold text made every table with a
rowspan/colspan fail coverage; a logical cell is covered if the union of its
member physical cells' native occurrences is non-empty.
"""
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
    p.add_argument('--run-root', type=Path, required=True)
    a = p.parse_args()
    root = a.run_root
    gate = json.loads((root / 'table_structure_gate.json').read_text())['tables']
    out = []
    for g in gate:
        if g['structure_gate'] != 'EXPLICIT_CANDIDATE':
            continue
        n = g['notice_id']
        target = json.loads((root / 'surya_targeted' / f'{n}_targeted_structure.json').read_text())
        pg = next(x for x in target['pages'] if x['page'] == g['page'])
        rec = pg['table_rec_tables'][g['table_index']]
        native = json.loads((root / 'raw/pdf_inspector' / f'{n}.json').read_text())
        render = json.loads((root / 'rendered' / n / 'render_manifest.json').read_text())
        h = next(x['height'] for x in render['pages'] if x['page'] == g['page'])
        scale = render['render_scale']
        bx, by, _, _ = rec['source_bbox']

        hs = [b for b in pg['high_accuracy_result']['blocks'] if b['label'] == 'Table']
        html_block = max(hs, key=lambda b: iou(b['bbox'], rec['source_bbox']), default=None)
        logical = parse_html_table_grid(html_block['html']) if html_block else {'rows': 0, 'cols': 0, 'cells': [], 'grid': {}}

        # Per-physical-cell native membership, same geometry as before.
        physical_members = {}
        for c in rec['result']['cells']:
            x0, y0, x1, y1 = c['bbox']
            members = []
            for i, item in enumerate(native['text_items']):
                if item['page'] != g['page']:
                    continue
                x = (item['x'] + item['width'] / 2) * scale
                y = h - (item['y'] + item['height'] / 2) * scale
                if bx + x0 <= x <= bx + x1 and by + y0 <= y <= by + y1:
                    members.append(i)
            physical_members[(c['row_id'], c['col_id'])] = {'physical_cell_id': c['cell_id'], 'bbox': c['bbox'], 'indices': members}

        # Fold physical cells into their owning logical (rowspan/colspan-aware)
        # cell. If any physical slot can't be resolved (grid size mismatch
        # despite the gate's row/col count check), bail out to PARTIAL rather
        # than guess.
        unresolved = False
        by_logical = {}
        for (row_id, col_id), phys in physical_members.items():
            logical_id = logical['grid'].get((row_id, col_id))
            if logical_id is None:
                unresolved = True
                continue
            by_logical.setdefault(logical_id, []).append(phys)

        cells = []
        if not unresolved and logical['cells']:
            for lc in logical['cells']:
                members = by_logical.get(lc['cell_id'], [])
                indices = sorted({i for m in members for i in m['indices']})
                cells.append({
                    'cell_id': lc['cell_id'], 'row': lc['row0'], 'column': lc['col0'],
                    'row_span': lc['row_span'], 'col_span': lc['col_span'],
                    'physical_cell_ids': [m['physical_cell_id'] for m in members],
                    'native_text_item_indices': indices,
                })

        covered = sum(bool(x['native_text_item_indices']) for x in cells)
        all_covered = bool(cells) and not unresolved and covered == len(cells)
        out.append({
            **{k: g[k] for k in ('notice_id', 'page', 'table_index', 'html_grid', 'tablerec_grid')},
            'cell_count': len(cells), 'native_covered_cells': covered,
            'all_cells_native_covered': all_covered,
            'status': 'EXPLICIT' if all_covered else 'PARTIAL',
            'cells': cells,
        })
    result = {'tables': out, 'explicit_count': sum(x['status'] == 'EXPLICIT' for x in out)}
    (root / 'table_cell_native_coverage.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'checked': len(out), 'explicit': result['explicit_count']}))


if __name__ == '__main__':
    main()
